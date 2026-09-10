"""What one account is allowed to use.

Two different questions, kept apart because their answers are. A quota asks whether an
account already holds as much of something as it may -- work in flight, bytes on disk --
and is fixed by finishing or deleting. A rate limit asks whether it is asking faster than
this service will answer, and is fixed by waiting a moment.

Both exist for the same reason: this service is one process shared by a handful of
people, and without them any one of them can make it useless for the rest by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from media_tool.core.logging import get_logger
from media_tool.domain.errors import QuotaExceededError, RateLimitedError

if TYPE_CHECKING:
    from media_tool.core.clock import Clock
    from media_tool.core.config import Settings
    from media_tool.domain.accounts import AccountId
    from media_tool.jobs.store import JobStore
    from media_tool.storage.base import ArtifactStore

logger = get_logger(__name__)

TOO_FAST = "too many requests; slow down and try again shortly"


class AccountQuotas:
    """Checks what an account already holds before letting it start more.

    Read at submit time and nowhere else. Enforcing partway through a job would leave
    half a batch done and no clear thing for the caller to do about it.
    """

    def __init__(self, *, jobs: JobStore, artifacts: ArtifactStore, settings: Settings) -> None:
        self._jobs = jobs
        self._artifacts = artifacts
        self._settings = settings

    async def check_can_submit(self, account: AccountId) -> None:
        """Refuse a submission that would put ``account`` over one of its limits.

        Raises:
            QuotaExceededError: naming which limit, because waiting for work to finish
                and deleting files are entirely different actions.
        """
        max_jobs = self._settings.max_active_jobs_per_account
        active = await self._jobs.count_active(account)
        if active >= max_jobs:
            msg = (
                f"you already have {active} unfinished jobs, which is the limit of "
                f"{max_jobs}; wait for one to finish"
            )
            raise QuotaExceededError(msg)

        max_bytes = self._settings.max_bytes_per_account
        used = self._artifacts.usage_bytes(account)
        if used >= max_bytes:
            msg = (
                f"you are storing {used} bytes, which is the limit of {max_bytes}; "
                f"delete a job to free some"
            )
            raise QuotaExceededError(msg)


@dataclass(slots=True)
class _Bucket:
    """One account's allowance, and when it was last measured."""

    tokens: float
    measured_at: float


class AccountRateLimiter:
    """A token bucket per account.

    Bursts are allowed up to the bucket's size, because the honest shape of this
    service's traffic is a batch submitted and then polled: refusing the burst would
    punish exactly the caller who is using the long poll correctly.

    One entry per account that has ever called, which is bounded by the number of people
    keyring has accounts for -- there is no path by which a caller invents an account id,
    so there is nothing here to grow without limit.
    """

    def __init__(self, *, clock: Clock, burst: int, per_second: float) -> None:
        self._clock = clock
        self._burst = float(burst)
        self._per_second = per_second
        self._buckets: dict[AccountId, _Bucket] = {}

    def check(self, account: AccountId) -> None:
        """Spend one of ``account``'s tokens, or refuse.

        Raises:
            RateLimitedError: if the account has none left.
        """
        now = self._clock.monotonic()
        bucket = self._buckets.get(account)

        if bucket is None:
            bucket = _Bucket(tokens=self._burst, measured_at=now)
            self._buckets[account] = bucket
        else:
            refilled = bucket.tokens + (now - bucket.measured_at) * self._per_second
            bucket.tokens = min(self._burst, refilled)
            bucket.measured_at = now

        if bucket.tokens < 1:
            logger.info("rate_limited", account=str(account))
            raise RateLimitedError(TOO_FAST)

        bucket.tokens -= 1


__all__ = ["AccountQuotas", "AccountRateLimiter"]
