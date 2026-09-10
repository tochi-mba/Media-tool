"""The job aggregate.

A job is one submitted batch. It owns its items' outcomes and derives its own status
from them, so no caller can put it into a state its items don't support. Everything
that mutates a job takes the current time as an argument -- the aggregate never reads a
clock itself.

A job also belongs to exactly one account, and is told which at creation. Carrying it on
the aggregate rather than passing it alongside is what makes "whose job is this" a
question with one answer: a store cannot file a job under an account the job disagrees
with, because there is nowhere for the second opinion to live.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from media_tool.domain.errors import InvalidJobTransitionError, JobItemNotFoundError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from media_tool.domain.accounts import AccountId
    from media_tool.domain.artifacts import DownloadArtifact
    from media_tool.domain.media import MediaQuery


class ItemStatus(StrEnum):
    """Where a single item in the batch got to."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    """Where the batch as a whole got to."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    """Some items succeeded and some failed. A distinct outcome, not a kind of failure."""

    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True when no further work will change this status."""
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class ItemError:
    """Why one item did not produce a file."""

    code: str
    """Stable, machine-readable reason. Safe for a client to branch on."""

    message: str


@dataclass(slots=True)
class JobItem:
    """One item's query and its outcome."""

    index: int
    query: MediaQuery
    status: ItemStatus = ItemStatus.PENDING
    artifact: DownloadArtifact | None = None
    error: ItemError | None = None

    @property
    def is_settled(self) -> bool:
        return self.status is not ItemStatus.PENDING


@dataclass(slots=True)
class Job:
    """A batch of downloads and everything known about its progress."""

    job_id: str
    account: AccountId
    """Whose job this is. Every read of it is checked against this, never against the id."""

    items: list[JobItem]
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    _cancelled: bool = field(default=False, repr=False)

    @classmethod
    def create(cls, *, account: AccountId, queries: Sequence[MediaQuery], now: datetime) -> Job:
        """Start a new queued job for ``account`` covering ``queries``.

        Raises:
            ValueError: if no queries were supplied.
        """
        if not queries:
            msg = "a job needs at least one item"
            raise ValueError(msg)

        return cls(
            job_id=uuid.uuid4().hex,
            account=account,
            items=[JobItem(index=index, query=query) for index, query in enumerate(queries)],
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )

    @property
    def item_count(self) -> int:
        return len(self.items)

    def item(self, index: int) -> JobItem:
        """Return the item at ``index``.

        Raises:
            JobItemNotFoundError: if the index is out of range. Negative indices are
                rejected rather than wrapping, since they only ever arrive from a URL.
        """
        if index < 0 or index >= len(self.items):
            msg = f"job {self.job_id} has no item at index {index}"
            raise JobItemNotFoundError(msg)
        return self.items[index]

    def start(self, *, now: datetime) -> None:
        """Mark the job as running.

        Raises:
            InvalidJobTransitionError: if the job has already left the queued state.
        """
        if self.status is not JobStatus.QUEUED:
            msg = f"cannot start job {self.job_id}: it is already {self.status.value}"
            raise InvalidJobTransitionError(msg)

        self.status = JobStatus.RUNNING
        self.started_at = now
        self.updated_at = now

    def record_success(self, index: int, artifact: DownloadArtifact, *, now: datetime) -> None:
        """Attach a captured artifact to an item."""
        item = self.item(index)
        if self._cancelled:
            return

        item.status = ItemStatus.SUCCEEDED
        item.artifact = artifact
        item.error = None
        self._settle(now=now)

    def record_failure(self, index: int, *, code: str, message: str, now: datetime) -> None:
        """Record why an item produced no file."""
        item = self.item(index)
        if self._cancelled:
            return

        item.status = ItemStatus.FAILED
        item.artifact = None
        item.error = ItemError(code=code, message=message)
        self._settle(now=now)

    def cancel(self, *, now: datetime) -> None:
        """Abandon whatever is still outstanding.

        Items that already finished keep their outcome -- a file that was captured is
        still a file that was captured.

        Raises:
            InvalidJobTransitionError: if the job has already reached a terminal state.
        """
        if self.status.is_terminal:
            msg = f"cannot cancel job {self.job_id}: it is already {self.status.value}"
            raise InvalidJobTransitionError(msg)

        self._cancelled = True
        for item in self.items:
            if not item.is_settled:
                item.status = ItemStatus.CANCELLED

        self.status = JobStatus.CANCELLED
        self.completed_at = now
        self.updated_at = now

    def counts(self) -> dict[str, int]:
        """Summarize progress. Always includes ``total``; other keys appear when non-zero."""
        counts = {"total": len(self.items)}
        for status in ItemStatus:
            tally = sum(1 for item in self.items if item.status is status)
            if tally:
                counts[status.value] = tally
        return counts

    def is_expired(self, *, now: datetime, ttl_seconds: float) -> bool:
        """Whether the job has been idle longer than its retention window.

        Measured from the last update rather than creation, so a long-running job is not
        evicted out from under its own runner.
        """
        return (now - self.updated_at).total_seconds() > ttl_seconds

    def _settle(self, *, now: datetime) -> None:
        """Recompute the job status from its items after one of them changed."""
        self.updated_at = now

        if any(not item.is_settled for item in self.items):
            return

        statuses = {item.status for item in self.items}
        if statuses == {ItemStatus.SUCCEEDED}:
            self.status = JobStatus.SUCCEEDED
        elif statuses == {ItemStatus.FAILED}:
            self.status = JobStatus.FAILED
        else:
            self.status = JobStatus.PARTIAL

        self.completed_at = now
