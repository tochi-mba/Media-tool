"""The in-memory job store, including the long-poll clients depend on."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from media_tool.domain.artifacts import DownloadArtifact
from media_tool.domain.errors import JobNotFoundError
from media_tool.domain.jobs import Job, JobStatus
from media_tool.domain.media import MediaQuery
from media_tool.jobs.store import InMemoryJobStore, JobStore
from tests.fakes.clock import EPOCH, FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> InMemoryJobStore:
    return InMemoryJobStore(clock=clock)


def make_job(clock: FakeClock, *names: str) -> Job:
    queries = [MediaQuery.create(name=name) for name in names or ("Dune",)]
    return Job.create(queries=queries, now=clock.now())


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

        assert (await store.get(job.job_id)).job_id == job.job_id

    async def test_an_unknown_job_is_not_found(self, store: InMemoryJobStore) -> None:
        with pytest.raises(JobNotFoundError, match="nope"):
            await store.get("nope")

    async def test_counting_reports_live_jobs(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        await store.add(make_job(clock))
        await store.add(make_job(clock))

        assert await store.count() == 2


class TestRetention:
    async def test_an_expired_job_reads_as_missing(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        clock.advance(timedelta(seconds=61))

        with pytest.raises(JobNotFoundError):
            await store.get(job.job_id, ttl_seconds=60)

    async def test_a_job_within_its_window_still_reads(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        clock.advance(timedelta(seconds=30))

        assert await store.get(job.job_id, ttl_seconds=60) is job

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
        waiter = asyncio.create_task(store.wait_for_terminal(job.job_id, timeout=5))
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

        settled = await store.wait_for_terminal(job.job_id, timeout=30)

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

        waiter = asyncio.create_task(store.wait_for_terminal(job.job_id, timeout=30))
        await asyncio.sleep(0)
        await finish()

        assert (await waiter).status is JobStatus.SUCCEEDED

    async def test_waiting_gives_up_and_returns_the_job_as_it_stands(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        unsettled = await store.wait_for_terminal(job.job_id, timeout=0.01)

        assert unsettled.status is JobStatus.QUEUED

    async def test_a_zero_timeout_does_not_wait(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)

        assert (await store.wait_for_terminal(job.job_id, timeout=0)).status is JobStatus.QUEUED

    async def test_waiting_on_an_unknown_job_is_not_found(self, store: InMemoryJobStore) -> None:
        with pytest.raises(JobNotFoundError):
            await store.wait_for_terminal("nope", timeout=1)

    async def test_several_waiters_are_all_released(
        self, store: InMemoryJobStore, clock: FakeClock
    ) -> None:
        job = make_job(clock)
        await store.add(job)
        job.start(now=clock.now())
        waiters = [
            asyncio.create_task(store.wait_for_terminal(job.job_id, timeout=30)) for _ in range(3)
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
