"""Executing a job.

One asyncio task per job; inside it, items are fanned out under a semaphore so a large
batch cannot open a hundred browser pages at once. Each item gets its own timeout and
its own retry budget, and the outcome is written back to the job as it lands rather than
at the end -- so a caller polling mid-flight sees real progress.

Time is injected (the clock, the sleeper, and the jitter function), so retry and timeout
behaviour is tested deterministically without a single real sleep.

Credentials are resolved at the start of each attempt and held in that attempt's local
scope. Never at submit time, and never on the job: a job outlives the request that made
it, gets read back, and is rendered into responses, so a credential stored on one would
be a credential in all three. It also means a retry after a long backoff re-checks the
caller's token rather than reusing one that has since expired.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from media_tool.core.logging import get_logger
from media_tool.domain.errors import (
    CredentialNotFoundError,
    KeyringUnavailableError,
    ReauthenticationRequiredError,
)
from media_tool.domain.jobs import JobStatus
from media_tool.providers.base import (
    ProviderCredentialError,
    ProviderCredentialMissingError,
    ProviderError,
    ProviderNotFoundError,
    ProviderReauthenticationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from media_tool.core.clock import Clock
    from media_tool.core.config import Settings
    from media_tool.core.keyring.credentials import Caller, CredentialSource, FormSecrets
    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.domain.jobs import Job, JobItem
    from media_tool.jobs.store import JobStore
    from media_tool.providers.base import DownloadProvider
    from media_tool.storage.local import LocalArtifactStore

logger = get_logger(__name__)

NO_KEYRING = "this service is running without keyring, so a site that needs a login cannot be used"

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
        credentials: CredentialSource | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] = _default_jitter,
    ) -> None:
        self._provider = provider
        self._jobs = job_store
        self._artifacts = artifact_store
        self._clock = clock
        self._settings = settings
        self._credentials = credentials
        self._sleep = sleeper
        self._jitter = jitter
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def in_flight(self) -> int:
        """How many jobs are currently executing."""
        return len(self._tasks)

    async def submit(self, job: Job, *, caller: Caller | None = None) -> None:
        """Start ``job`` in the background and return immediately.

        ``caller`` carries the token this job's credentials will be resolved with. It
        lives in the task and in nothing that is stored.
        """
        task = asyncio.create_task(self.run(job, caller=caller), name=f"download-job-{job.job_id}")
        self._tasks[job.job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.job_id, None))

    async def run(self, job: Job, *, caller: Caller | None = None) -> None:
        """Execute every item in ``job``, recording outcomes as they land."""
        job.start(now=self._clock.now())
        await self._jobs.save(job)

        semaphore = asyncio.Semaphore(self._settings.download_concurrency)
        groups = _group_by_key(job.items)

        try:
            await asyncio.gather(
                *(self._run_group(job, items, semaphore, caller) for items in groups.values())
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
        self,
        job: Job,
        items: Sequence[JobItem],
        semaphore: asyncio.Semaphore,
        caller: Caller | None,
    ) -> None:
        """Download once for a group of identical queries, then fan the result out."""
        leader, *duplicates = items

        async with semaphore:
            try:
                artifact = await self._download_with_retries(job, leader, caller)
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
                account=job.account,
                job_id=job.job_id,
                source_index=leader.index,
                target_index=duplicate.index,
                filename=artifact.filename,
            )
            job.record_success(duplicate.index, copied, now=self._clock.now())

        await self._jobs.save(job)

    async def _download_with_retries(
        self, job: Job, item: JobItem, caller: Caller | None
    ) -> DownloadArtifact:
        """Attempt one item, backing off between tries.

        A miss is not retried: looking again will not make the site have the file. Nor is
        a credential failure: every one of those needs somebody to do something -- sign in
        again, connect an account, store another field -- and none is fixed by asking
        twice.
        """
        last_error: ProviderError | None = None

        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                return await self._attempt(job, item, caller)
            except (ProviderNotFoundError, ProviderCredentialError):
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

    async def _attempt(self, job: Job, item: JobItem, caller: Caller | None) -> DownloadArtifact:
        """One try, bounded by the per-item timeout.

        Credentials are resolved first, outside the timeout: keyring has its own, and a
        credential problem reported as "the download timed out" would send whoever reads
        it looking in the wrong place.
        """
        secrets = await self._secrets_for(caller)

        try:
            async with asyncio.timeout(self._settings.download_timeout_seconds):
                with self._artifacts.reserve(
                    account=job.account, job_id=job.job_id, index=item.index
                ) as sink:
                    return await self._provider.download(
                        query=item.query, sink=sink, secrets=secrets
                    )
        except TimeoutError as error:
            msg = (
                f"download exceeded {self._settings.download_timeout_seconds:g}s "
                f"for {item.query.name!r}"
            )
            raise ProviderTimeoutError(msg) from error

    async def _secrets_for(self, caller: Caller | None) -> FormSecrets | None:
        """Read the stored login this attempt needs, if the provider needs one.

        Every way this can fail is translated into a provider error here, so that the
        runner's one classification decides the item's outcome and the job never sees a
        keyring exception type.
        """
        service = self._provider.requires_login
        if service is None:
            return None

        if self._credentials is None or caller is None:
            raise ProviderCredentialMissingError(NO_KEYRING)

        try:
            return await self._credentials.form_secrets(
                user_token=caller.token, profile=caller.profile, service=service
            )
        except ReauthenticationRequiredError as error:
            raise ProviderReauthenticationError(str(error)) from error
        except CredentialNotFoundError as error:
            raise ProviderCredentialMissingError(str(error)) from error
        except KeyringUnavailableError as error:
            # The one credential failure worth retrying: an outage passes.
            raise ProviderUnavailableError(str(error)) from error

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
