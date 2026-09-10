"""The in-memory job store, including the long-poll clients depend on."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from media_tool.domain.accounts import AccountId
from media_tool.domain.artifacts import DownloadArtifact
from media_tool.domain.errors import JobNotFoundError
from media_tool.domain.jobs import Job, JobStatus
from media_tool.domain.media import MediaQuery
from media_tool.jobs.store import InMemoryJobStore, JobStore
from tests.fakes.accounts import ALICE, BOB
from tests.fakes.clock import EPOCH, FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> InMemoryJobStore:
    return InMemoryJobStore(clock=clock)


def make_job(clock: FakeClock, *names: str, account: AccountId = ALICE) -> Job:
    queries = [MediaQuery.create(name=name) for name in names or ("Dune",)]
    return Job.create(account=account, queries=queries, now=clock.now())


def artifact() -> DownloadArtifact:
    return DownloadArtifact(
        filename="a.bin",
        size_bytes=1,
        content_type="application/octet-stream",
        sha256="0" * 64,
        source_url="stub://a",
        duration_seconds=0.1,
        downloaded_at=EPOCH,
    )


class TestPortConformance:
    def test_the_in_memory_store_satisfies_the_port(self, store: InMemoryJobStore) -> None:
        checked: JobStore = store

        assert checked is store


class TestRoundTrip:
    async def test_a_stored_job_can_be_read_back(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)

        await store.add(job)

        assert (await store.get(job.job_id, account=ALICE)).job_id == job.job_id

    async def test_an_unknown_job_is_not_found(self, store: InMemoryJobStore) -> None:
        with pytest.raises(JobNotFoundError, match="nope"):
            await store.get("nope", account=ALICE)

    async def test_counting_reports_live_jobs(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        await store.add(make_job(clock))
        await store.add(make_job(clock))

        assert await store.count() == 2


class TestIsolation:
    """One account's jobs are not another's, and asking is not a way to find out.

    Every test here is named for the property rather than the mechanism, because the
    mechanism is allowed to change and the property is not.
    """

    async def test_another_accounts_job_reads_as_missing(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock, account=ALICE)
        await store.add(job)

        with pytest.raises(JobNotFoundError):
            await store.get(job.job_id, account=BOB)

    async def test_a_job_id_is_not_a_capability(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        # Job ids are uuid4, but unguessable is not the same as checked, and only one of
        # the two is a rule. This is the test that says which.
        job = make_job(clock, account=ALICE)
        await store.add(job)

        with pytest.raises(JobNotFoundError):
            await store.get(job.job_id, account=BOB, ttl_seconds=60)

    async def test_the_refusal_is_indistinguishable_from_a_missing_job(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        # A different error, or a different message, would confirm the id is real --
        # which is the one thing somebody guessing at ids wants to know.
        job = make_job(clock, account=ALICE)
        await store.add(job)

        with pytest.raises(JobNotFoundError) as theirs:
            await store.get(job.job_id, account=BOB)
        with pytest.raises(JobNotFoundError) as absent:
            await store.get("does-not-exist", account=BOB)

        assert str(theirs.value).replace(job.job_id, "X") == str(absent.value).replace(
            "does-not-exist", "X"
        )

    async def test_waiting_on_another_accounts_job_reads_as_missing(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        # The long poll is a second door into the same room, so it gets the same lock.
        job = make_job(clock, account=ALICE)
        await store.add(job)

        with pytest.raises(JobNotFoundError):
            await store.wait_for_terminal(job.job_id, account=BOB, timeout=0)

    async def test_each_account_reads_its_own(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        hers = make_job(clock, "Dune", account=ALICE)
        his = make_job(clock, "Arrival", account=BOB)
        await store.add(hers)
        await store.add(his)

        assert (await store.get(hers.job_id, account=ALICE)).job_id == hers.job_id
        assert (await store.get(his.job_id, account=BOB)).job_id == his.job_id

    async def test_the_count_is_of_the_process_not_of_a_caller(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        # /healthy reports on the service. It is the one number that is not per-account,
        # and it holds no job ids, so it tells nobody anything about anybody.
        await store.add(make_job(clock, account=ALICE))
        await store.add(make_job(clock, account=BOB))

        assert await store.count() == 2


class TestRetention:
    async def test_an_expired_job_reads_as_missing(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        clock.advance(timedelta(seconds=61))

        with pytest.raises(JobNotFoundError):
            await store.get(job.job_id, ttl_seconds=60, account=ALICE)

    async def test_a_job_within_its_window_still_reads(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        clock.advance(timedelta(seconds=30))

        assert await store.get(job.job_id, ttl_seconds=60, account=ALICE) is job

    async def test_purging_returns_the_ids_it_dropped(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        old = make_job(clock)
        await store.add(old)
        clock.advance(timedelta(seconds=61))
        fresh = make_job(clock)
        await store.add(fresh)

        purged = await store.purge_expired(ttl_seconds=60)

        assert purged == [old.job_id]
        assert await store.count() == 1

    async def test_purging_an_empty_store_is_harmless(self, store: InMemoryJobStore) -> None:
        assert await store.purge_expired(ttl_seconds=60) == []

    async def test_waiters_on_a_purged_job_are_released(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)
        waiter = asyncio.create_task(store.wait_for_terminal(job.job_id, timeout=5, account=ALICE))
        await asyncio.sleep(0)

        clock.advance(timedelta(seconds=61))
        await store.purge_expired(ttl_seconds=60)

        with pytest.raises(JobNotFoundError):
            await waiter


class TestLongPoll:
    async def test_a_job_that_is_already_finished_returns_at_once(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        job.start(now=clock.now())
        job.record_success(0, artifact(), now=clock.now())
        await store.add(job)

        settled = await store.wait_for_terminal(job.job_id, timeout=30, account=ALICE)

        assert settled.status is JobStatus.SUCCEEDED

    async def test_waiting_returns_as_soon_as_the_job_settles(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)
        job.start(now=clock.now())

        async def finish() -> None:
            job.record_success(0, artifact(), now=clock.now())
            await store.save(job)

        waiter = asyncio.create_task(store.wait_for_terminal(job.job_id, timeout=30, account=ALICE))
        await asyncio.sleep(0)
        await finish()

        assert (await waiter).status is JobStatus.SUCCEEDED

    async def test_waiting_gives_up_and_returns_the_job_as_it_stands(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        unsettled = await store.wait_for_terminal(job.job_id, timeout=0.01, account=ALICE)

        assert unsettled.status is JobStatus.QUEUED

    async def test_a_zero_timeout_does_not_wait(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        assert (
            await store.wait_for_terminal(job.job_id, timeout=0, account=ALICE)
        ).status is JobStatus.QUEUED

    async def test_waiting_on_an_unknown_job_is_not_found(self, store: InMemoryJobStore) -> None:
        with pytest.raises(JobNotFoundError):
            await store.wait_for_terminal("nope", timeout=1, account=ALICE)

    async def test_several_waiters_are_all_released(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)
        job.start(now=clock.now())
        waiters = [
            asyncio.create_task(store.wait_for_terminal(job.job_id, timeout=30, account=ALICE))
            for _ in range(3)
        ]
        await asyncio.sleep(0)

        job.record_success(0, artifact(), now=clock.now())
        await store.save(job)

        assert all(
            result.status is JobStatus.SUCCEEDED for result in await asyncio.gather(*waiters)
        )


class TestConcurrency:
    async def test_concurrent_writes_all_land(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        jobs = [make_job(clock) for _ in range(25)]

        await asyncio.gather(*(store.add(job) for job in jobs))

        assert await store.count() == 25
