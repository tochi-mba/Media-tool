"""Executing a job.

One asyncio task per job; inside it, items are fanned out under a semaphore so a large
batch cannot open a hundred browser pages at once. Each item gets its own timeout and
its own retry budget, and the outcome is written back to the job as it lands rather than
at the end -- so a caller polling mid-flight sees real progress.

Time is injected (the clock, the sleeper, and the jitter function), so retry and timeout
behaviour is tested deterministically without a single real sleep.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from media_tool.core.logging import get_logger
from media_tool.domain.jobs import JobStatus
from media_tool.providers.base import (
    ProviderError,
    ProviderNotFoundError,
    ProviderTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from media_tool.core.clock import Clock
    from media_tool.core.config import Settings
    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.domain.jobs import Job, JobItem
    from media_tool.jobs.store import JobStore
    from media_tool.providers.base import DownloadProvider
    from media_tool.storage.local import LocalArtifactStore

logger = get_logger(__name__)

INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_MESSAGE = "an unexpected error occurred while downloading"
"""Deliberately vague: an unexpected exception's text can carry paths or credentials."""

Sleeper = "Callable[[float], Awaitable[None]]"


def _default_jitter(delay: float) -> float:
    """Spread retries so a batch that fails together does not retry in lockstep."""
    return delay * random.uniform(0.5, 1.0)  # noqa: S311 - scheduling, not cryptography


class DownloadJobRunner:
    """Runs jobs against a provider."""

    def __init__(  # noqa: PLR0913 - a composition root injects every collaborator by name
        self,
        *,
        provider: DownloadProvider,
        job_store: JobStore,
        artifact_store: LocalArtifactStore,
        clock: Clock,
        settings: Settings,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] = _default_jitter,
    ) -> None:
        self._provider = provider
        self._jobs = job_store
        self._artifacts = artifact_store
        self._clock = clock
        self._settings = settings
        self._sleep = sleeper
        self._jitter = jitter
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def in_flight(self) -> int:
        """How many jobs are currently executing."""
        return len(self._tasks)

    async def submit(self, job: Job) -> None:
        """Start ``job`` in the background and return immediately."""
        task = asyncio.create_task(self.run(job), name=f"download-job-{job.job_id}")
        self._tasks[job.job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.job_id, None))

    async def run(self, job: Job) -> None:
        """Execute every item in ``job``, recording outcomes as they land."""
        job.start(now=self._clock.now())
        await self._jobs.save(job)

        semaphore = asyncio.Semaphore(self._settings.download_concurrency)
        groups = _group_by_key(job.items)

        try:
            await asyncio.gather(
                *(self._run_group(job, items, semaphore) for items in groups.values())
            )
        except asyncio.CancelledError:
            if not job.status.is_terminal:
                job.cancel(now=self._clock.now())
                await self._jobs.save(job)
            raise

        await self._jobs.save(job)

    async def cancel(self, job: Job) -> None:
        """Stop a job, whether or not it is currently executing."""
        task = self._tasks.get(job.job_id)
        if task is not None:
            task.cancel()

        if not job.status.is_terminal:
            job.cancel(now=self._clock.now())

        await self._jobs.save(job)

    async def drain(self, *, timeout: float) -> bool:  # noqa: ASYNC109 - a partial drain is a normal outcome, not a cancellation
        """Wait for in-flight jobs to finish. Returns whether they all did.

        The graceful half of shutdown: a download that is nearly done is worth the few
        seconds it needs, where cancelling it wastes the whole transfer.
        """
        if not self._tasks:
            return True

        pending = list(self._tasks.values())
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        return not still_running

    async def aclose(self, *, drain_timeout: float = 0.0) -> None:
        """Stop work and release the provider. Safe to call twice.

        Gives in-flight jobs ``drain_timeout`` seconds to finish before cancelling them.
        """
        if self._closed:
            return
        self._closed = True

        if drain_timeout > 0:
            await self.drain(timeout=drain_timeout)

        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        await self._provider.aclose()

    async def _run_group(
        self, job: Job, items: Sequence[JobItem], semaphore: asyncio.Semaphore
    ) -> None:
        """Download once for a group of identical queries, then fan the result out."""
        leader, *duplicates = items

        async with semaphore:
            try:
                artifact = await self._download_with_retries(job, leader)
            except ProviderError as error:
                self._record_failure(job, items, code=error.code, message=str(error))
                await self._jobs.save(job)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("download_failed_unexpectedly", job_id=job.job_id)
                self._record_failure(
                    job, items, code=INTERNAL_ERROR_CODE, message=INTERNAL_ERROR_MESSAGE
                )
                await self._jobs.save(job)
                return

        job.record_success(leader.index, artifact, now=self._clock.now())
        for duplicate in duplicates:
            copied = self._artifacts.link_artifact(
                job_id=job.job_id,
                source_index=leader.index,
                target_index=duplicate.index,
                filename=artifact.filename,
            )
            job.record_success(duplicate.index, copied, now=self._clock.now())

        await self._jobs.save(job)

    async def _download_with_retries(self, job: Job, item: JobItem) -> DownloadArtifact:
        """Attempt one item, backing off between tries.

        A miss is not retried: looking again will not make the site have the file.
        """
        last_error: ProviderError | None = None

        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                return await self._attempt(job, item)
            except ProviderNotFoundError:
                raise
            except ProviderError as error:
                last_error = error
                logger.warning(
                    "download_attempt_failed",
                    job_id=job.job_id,
                    index=item.index,
                    attempt=attempt,
                    error=str(error),
                )
                if attempt < self._settings.max_attempts:
                    await self._sleep(self._backoff_for(attempt))

        assert last_error is not None  # noqa: S101 - the loop always sets it before exiting
        raise last_error

    async def _attempt(self, job: Job, item: JobItem) -> DownloadArtifact:
        """One try, bounded by the per-item timeout."""
        try:
            async with asyncio.timeout(self._settings.download_timeout_seconds):
                with self._artifacts.reserve(job_id=job.job_id, index=item.index) as sink:
                    return await self._provider.download(query=item.query, sink=sink)
        except TimeoutError as error:
            msg = (
                f"download exceeded {self._settings.download_timeout_seconds:g}s "
                f"for {item.query.name!r}"
            )
            raise ProviderTimeoutError(msg) from error

    def _backoff_for(self, attempt: int) -> float:
        """Exponential backoff, capped, then jittered."""
        delay = self._settings.backoff_base_seconds * (2 ** (attempt - 1))
        return self._jitter(min(delay, self._settings.backoff_max_seconds))

    def _record_failure(
        self, job: Job, items: Sequence[JobItem], *, code: str, message: str
    ) -> None:
        now = self._clock.now()
        for item in items:
            job.record_failure(item.index, code=code, message=message, now=now)


def _group_by_key(items: Sequence[JobItem]) -> dict[str, list[JobItem]]:
    """Group items by canonical query key, preserving submission order within a group."""
    groups: dict[str, list[JobItem]] = {}
    for item in items:
        groups.setdefault(item.query.key, []).append(item)
    return groups


__all__ = ["DownloadJobRunner", "JobStatus"]
