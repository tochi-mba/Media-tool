"""Where jobs live between the request that creates them and the request that reads them.

The port exists so this can become Redis or Postgres later without anything else
changing. The in-memory implementation is honest about its limits: jobs die with the
process and do not span replicas (see ADR-0003).

It also owns the long-poll, because it is the only component that knows when a job
settles. That lets a caller -- an AI assistant calling this as a tool, especially -- make
one request instead of a polling loop.

Every read takes the account making it, and a job belonging to somebody else is reported
exactly as one that does not exist. A 403 would be worse than useless here: it confirms
the job id is real, which is the one thing a caller guessing at ids wants to know.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from media_tool.domain.errors import JobNotFoundError

if TYPE_CHECKING:
    from media_tool.core.clock import Clock
    from media_tool.domain.accounts import AccountId
    from media_tool.domain.jobs import Job


class JobStore(Protocol):
    """Persistence for jobs."""

    async def add(self, job: Job) -> None:
        """Store a newly created job."""
        ...

    async def get(
        self, job_id: str, *, account: AccountId, ttl_seconds: float | None = None
    ) -> Job:
        """Return one of ``account``'s jobs.

        Raises:
            JobNotFoundError: if it is unknown, belongs to another account, or is older
                than ``ttl_seconds``. One error for all three, deliberately.
        """
        ...

    async def save(self, job: Job) -> None:
        """Record a change to a job, releasing anyone waiting on it if it has settled."""
        ...

    async def wait_for_terminal(
        self,
        job_id: str,
        *,
        account: AccountId,
        timeout: float,  # noqa: ASYNC109
    ) -> Job:
        """Return the job once it reaches a terminal state, or when ``timeout`` elapses.

        Returns the job either way; callers check the status. A timeout is a normal
        outcome, not an error.

        Raises:
            JobNotFoundError: as :meth:`get`, and for the same reasons. Waiting on
                somebody else's job is not a way to learn that it exists.
        """
        ...

    async def purge_expired(self, *, ttl_seconds: float) -> list[Job]:
        """Drop jobs idle longer than ``ttl_seconds``. Returns the jobs that went.

        The jobs rather than their ids, because whoever cleans up after them needs to
        know whose they were: an artifact lives under its account, and an id alone no
        longer names a file.
        """
        ...

    async def count(self) -> int:
        """How many jobs are currently held, across every account.

        For ``/healthy``, which reports on the process rather than on a caller.
        """
        ...


class InMemoryJobStore:
    """Holds jobs in a dictionary guarded by an asyncio lock."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._settled: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def add(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job
            self._settled[job.job_id] = asyncio.Event()

    async def get(
        self, job_id: str, *, account: AccountId, ttl_seconds: float | None = None
    ) -> Job:
        async with self._lock:
            return self._get_locked(job_id, account=account, ttl_seconds=ttl_seconds)

    async def save(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job
            if job.status.is_terminal and (event := self._settled.get(job.job_id)) is not None:
                event.set()

    async def wait_for_terminal(
        self,
        job_id: str,
        *,
        account: AccountId,
        timeout: float,  # noqa: ASYNC109
    ) -> Job:
        # ASYNC109 asks callers to wrap with asyncio.timeout instead of taking a timeout
        # argument. That does not fit here: elapsing is a normal outcome that returns the
        # job as it stands, not a cancellation, and the duration is part of the HTTP
        # contract (the wait_seconds query parameter).
        async with self._lock:
            job = self._get_locked(job_id, account=account, ttl_seconds=None)
            event = self._settled[job_id]

        if not job.status.is_terminal and timeout > 0:
            # A timeout here is the expected outcome for a job that is still working;
            # the caller gets the job as it currently stands.
            try:
                async with asyncio.timeout(timeout):
                    await event.wait()
            except TimeoutError:
                pass

        # Re-read: the job may have been purged while we waited.
        return await self.get(job_id, account=account)

    async def purge_expired(self, *, ttl_seconds: float) -> list[Job]:
        now = self._clock.now()

        async with self._lock:
            expired = [
                job
                for job in self._jobs.values()
                if job.is_expired(now=now, ttl_seconds=ttl_seconds)
            ]
            for job in expired:
                del self._jobs[job.job_id]
                # Release anyone still waiting; they will re-read and find it gone.
                self._settled.pop(job.job_id).set()

        return expired

    async def count(self) -> int:
        async with self._lock:
            return len(self._jobs)

    def _get_locked(self, job_id: str, *, account: AccountId, ttl_seconds: float | None) -> Job:
        job = self._jobs.get(job_id)
        if job is None or job.account != account:
            # One message for both, because they are one answer: as far as this caller is
            # concerned, there is no such job. Job ids are uuid4, but being unguessable is
            # not the same as being checked, and only one of the two is a rule.
            msg = f"no job {job_id!r}"
            raise JobNotFoundError(msg)

        if ttl_seconds is not None and job.is_expired(
            now=self._clock.now(), ttl_seconds=ttl_seconds
        ):
            msg = f"job {job_id!r} has passed its retention window"
            raise JobNotFoundError(msg)

        return job
