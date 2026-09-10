"""Per-account limits.

This service is one process shared by a handful of people. These are what stop any one
of them making it useless for the rest by accident -- a hundred queued downloads, a full
disk, a polling loop with no sleep in it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from media_tool.core.config import Settings
from media_tool.core.limits import AccountQuotas, AccountRateLimiter
from media_tool.domain.errors import QuotaExceededError, RateLimitedError
from media_tool.domain.jobs import Job
from media_tool.domain.media import MediaQuery
from media_tool.jobs.store import InMemoryJobStore
from media_tool.storage.local import LocalArtifactStore
from tests.fakes.accounts import ALICE, BOB
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def jobs(clock: FakeClock) -> InMemoryJobStore:
    return InMemoryJobStore(clock=clock)


@pytest.fixture
def artifacts(tmp_path: Path, clock: FakeClock) -> LocalArtifactStore:
    return LocalArtifactStore(root=tmp_path / "artifacts", clock=clock, max_file_bytes=1_000_000)


def settings_with(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        require_authentication=False,
        **overrides,  # type: ignore[arg-type]
    )


def quotas(
    jobs: InMemoryJobStore, artifacts: LocalArtifactStore, **overrides: object
) -> AccountQuotas:
    return AccountQuotas(jobs=jobs, artifacts=artifacts, settings=settings_with(**overrides))


async def fill_jobs(jobs: InMemoryJobStore, clock: FakeClock, count: int) -> list[Job]:
    made = []
    for _ in range(count):
        job = Job.create(account=ALICE, queries=[MediaQuery.create(name="Dune")], now=clock.now())
        await jobs.add(job)
        made.append(job)
    return made


def write_bytes(artifacts: LocalArtifactStore, size: int, *, name: str = "big.bin") -> None:
    with artifacts.reserve(account=ALICE, job_id="job1", index=0) as sink:
        sink.staging_path.write_bytes(b"x" * size)
        sink.commit(
            suggested_filename=name,
            content_type="application/octet-stream",
            source_url="https://example.test/big",
        )


class TestConcurrentJobs:
    async def test_submitting_under_the_limit_is_allowed(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        await fill_jobs(jobs, clock, 2)

        await quotas(jobs, artifacts, max_active_jobs_per_account=3).check_can_submit(ALICE)

    async def test_submitting_at_the_limit_is_refused(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        await fill_jobs(jobs, clock, 3)

        with pytest.raises(QuotaExceededError):
            await quotas(jobs, artifacts, max_active_jobs_per_account=3).check_can_submit(ALICE)

    async def test_the_refusal_names_the_limit_and_the_fix(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # Waiting for work to finish and deleting files are different actions, so a
        # message that did not say which would leave the caller guessing.
        await fill_jobs(jobs, clock, 3)

        with pytest.raises(QuotaExceededError, match="wait for one to finish"):
            await quotas(jobs, artifacts, max_active_jobs_per_account=3).check_can_submit(ALICE)

    async def test_finished_jobs_do_not_count_against_it(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        made = await fill_jobs(jobs, clock, 3)
        for job in made:
            job.cancel(now=clock.now())
            await jobs.save(job)

        await quotas(jobs, artifacts, max_active_jobs_per_account=3).check_can_submit(ALICE)

    async def test_another_accounts_jobs_do_not_count_against_it(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        await fill_jobs(jobs, clock, 3)

        await quotas(jobs, artifacts, max_active_jobs_per_account=3).check_can_submit(BOB)


class TestDiskUsage:
    def test_an_account_with_nothing_stored_uses_nothing(
        self, artifacts: LocalArtifactStore
    ) -> None:
        assert artifacts.usage_bytes(ALICE) == 0

    def test_usage_is_what_that_account_stored(self, artifacts: LocalArtifactStore) -> None:
        write_bytes(artifacts, 500)

        assert artifacts.usage_bytes(ALICE) == 500
        assert artifacts.usage_bytes(BOB) == 0

    async def test_being_over_the_byte_limit_refuses_a_submission(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore
    ) -> None:
        write_bytes(artifacts, 500)

        with pytest.raises(QuotaExceededError, match="delete a job"):
            await quotas(jobs, artifacts, max_bytes_per_account=400).check_can_submit(ALICE)

    async def test_the_two_limits_trip_independently(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # Room on disk but no room for another job, and the message says so.
        await fill_jobs(jobs, clock, 3)
        write_bytes(artifacts, 10)

        checker = quotas(
            jobs, artifacts, max_active_jobs_per_account=3, max_bytes_per_account=1_000_000
        )
        with pytest.raises(QuotaExceededError, match="unfinished jobs"):
            await checker.check_can_submit(ALICE)


class TestRateLimiting:
    def limiter(
        self, clock: FakeClock, *, burst: int = 3, per_second: float = 1.0
    ) -> AccountRateLimiter:
        return AccountRateLimiter(clock=clock, burst=burst, per_second=per_second)

    def test_a_burst_is_allowed(self, clock: FakeClock) -> None:
        # The honest shape of this traffic is a batch submitted and then polled, so a
        # small burst allowance would punish the caller using the long poll correctly.
        limiter = self.limiter(clock, burst=3)

        for _ in range(3):
            limiter.check(ALICE)

    def test_going_past_the_burst_is_refused(self, clock: FakeClock) -> None:
        limiter = self.limiter(clock, burst=3)
        for _ in range(3):
            limiter.check(ALICE)

        with pytest.raises(RateLimitedError):
            limiter.check(ALICE)

    def test_waiting_earns_another_request(self, clock: FakeClock) -> None:
        limiter = self.limiter(clock, burst=3, per_second=1.0)
        for _ in range(3):
            limiter.check(ALICE)

        clock.advance(1)

        limiter.check(ALICE)

    def test_the_allowance_does_not_grow_past_the_burst(self, clock: FakeClock) -> None:
        # An account that has been idle all day gets a burst, not a day's worth.
        limiter = self.limiter(clock, burst=3, per_second=1.0)
        clock.advance(86_400)

        for _ in range(3):
            limiter.check(ALICE)
        with pytest.raises(RateLimitedError):
            limiter.check(ALICE)

    def test_one_account_cannot_spend_anothers_allowance(self, clock: FakeClock) -> None:
        limiter = self.limiter(clock, burst=1)
        limiter.check(ALICE)

        limiter.check(BOB)
