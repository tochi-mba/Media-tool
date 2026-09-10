"""The job runner: concurrency, timeouts, retries, deduplication, and cancellation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from media_tool.core.config import Settings
from media_tool.domain.jobs import ItemStatus, Job, JobStatus
from media_tool.domain.media import MediaQuery
from media_tool.jobs.runner import DownloadJobRunner, _default_jitter
from media_tool.jobs.store import InMemoryJobStore
from media_tool.providers.base import (
    ProviderNotFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from media_tool.storage.local import LocalArtifactStore
from tests.fakes.accounts import ALICE
from tests.fakes.clock import FakeClock
from tests.fakes.provider import FakeProvider

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def artifacts(tmp_path: Path, clock: FakeClock) -> LocalArtifactStore:
    return LocalArtifactStore(root=tmp_path / "a", clock=clock, max_file_bytes=1_000_000)


@pytest.fixture
def jobs(clock: FakeClock) -> InMemoryJobStore:
    return InMemoryJobStore(clock=clock)


def settings_with(**overrides: object) -> Settings:
    base: dict[str, object] = {
        # Production-shaped, so nothing here quietly tests an unauthenticated service.
        "keyring_base_url": "https://keyring.test",
        "keyring_service_token": "service-token-for-media-tool",
        "download_concurrency": 2,
        "max_attempts": 3,
        "backoff_base_seconds": 0.1,
        "backoff_max_seconds": 1.0,
        "download_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg,arg-type]


def make_runner(
    provider: FakeProvider,
    jobs: InMemoryJobStore,
    artifacts: LocalArtifactStore,
    clock: FakeClock,
    **settings: object,
) -> DownloadJobRunner:
    slept: list[float] = []

    async def sleeper(delay: float) -> None:
        slept.append(delay)

    runner = DownloadJobRunner(
        provider=provider,
        job_store=jobs,
        artifact_store=artifacts,
        clock=clock,
        settings=settings_with(**settings),
        sleeper=sleeper,
        jitter=lambda delay: delay,
    )
    runner.slept = slept  # type: ignore[attr-defined]
    return runner


async def make_job(jobs: InMemoryJobStore, clock: FakeClock, *names: str) -> Job:
    job = Job.create(
        account=ALICE, queries=[MediaQuery.create(name=name) for name in names], now=clock.now()
    )
    await jobs.add(job)
    return job


class TestHappyPath:
    async def test_every_item_gets_a_file(
        self,
        jobs: InMemoryJobStore,
        artifacts: LocalArtifactStore,
        clock: FakeClock,
    ) -> None:
        provider = FakeProvider()
        runner = make_runner(provider, jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune", "Arrival")

        await runner.run(job)

        assert job.status is JobStatus.SUCCEEDED
        for item in job.items:
            assert item.artifact is not None
            assert artifacts.locate(
                job_id=job.job_id, index=item.index, filename=item.artifact.filename
            ).exists()

    async def test_the_job_is_marked_running_then_settled(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert job.started_at is not None
        assert job.completed_at is not None


class TestConcurrency:
    async def test_the_concurrency_cap_is_respected(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider()
        runner = make_runner(provider, jobs, artifacts, clock, download_concurrency=2)
        job = await make_job(jobs, clock, "A", "B", "C", "D", "E", "F")

        await runner.run(job)

        assert provider.max_concurrent <= 2

    async def test_a_cap_of_one_serializes(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider()
        runner = make_runner(provider, jobs, artifacts, clock, download_concurrency=1)
        job = await make_job(jobs, clock, "A", "B", "C")

        await runner.run(job)

        assert provider.max_concurrent == 1


class TestDeduplication:
    async def test_a_repeated_query_is_downloaded_once(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider()
        runner = make_runner(provider, jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune", "Arrival", "Dune")

        await runner.run(job)

        assert sorted(provider.calls) == ["unknown:arrival", "unknown:dune"]

    async def test_every_duplicate_still_gets_its_own_result_and_file(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune", "Arrival", "Dune")

        await runner.run(job)

        first, _, duplicate = job.items
        assert first.artifact is not None
        assert duplicate.artifact is not None
        assert duplicate.artifact.sha256 == first.artifact.sha256
        assert artifacts.locate(
            job_id=job.job_id, index=2, filename=duplicate.artifact.filename
        ).exists()

    async def test_a_failure_is_fanned_out_to_every_duplicate(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(fail_with={"unknown:dune": ProviderNotFoundError("nothing here")})
        runner = make_runner(provider, jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune", "Dune")

        await runner.run(job)

        assert [item.status for item in job.items] == [ItemStatus.FAILED] * 2
        assert job.status is JobStatus.FAILED


class TestFailures:
    async def test_a_miss_is_recorded_with_its_code(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(fail_with={"unknown:dune": ProviderNotFoundError("no match")})
        runner = make_runner(provider, jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert job.items[0].error is not None
        assert job.items[0].error.code == "not_found"
        assert job.items[0].error.message == "no match"

    async def test_one_failure_among_successes_is_partial(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(fail_with={"unknown:dune": ProviderNotFoundError("no")})
        runner = make_runner(provider, jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune", "Arrival")

        await runner.run(job)

        assert job.status is JobStatus.PARTIAL

    async def test_an_unexpected_error_does_not_take_the_job_down(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(fail_with={"unknown:dune": RuntimeError("kaboom")})
        runner = make_runner(provider, jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune", "Arrival")

        await runner.run(job)

        assert job.items[0].error is not None
        assert job.items[0].error.code == "internal_error"
        assert job.items[1].status is ItemStatus.SUCCEEDED

    async def test_an_unexpected_error_message_is_not_leaked_to_the_client(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(
            fail_with={"unknown:dune": RuntimeError("/srv/secret/path exploded")}
        )
        runner = make_runner(provider, jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert job.items[0].error is not None
        assert "secret" not in job.items[0].error.message


class TestRetries:
    async def test_a_transient_failure_is_retried_and_succeeds(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(fail_times={"unknown:dune": 2})
        runner = make_runner(provider, jobs, artifacts, clock, max_attempts=3)
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert job.status is JobStatus.SUCCEEDED
        assert provider.calls == ["unknown:dune"] * 3

    async def test_attempts_are_capped(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(fail_times={"unknown:dune": 99})
        runner = make_runner(provider, jobs, artifacts, clock, max_attempts=3)
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert job.status is JobStatus.FAILED
        assert len(provider.calls) == 3

    async def test_backoff_grows_and_is_capped(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(fail_times={"unknown:dune": 99})
        runner = make_runner(
            provider,
            jobs,
            artifacts,
            clock,
            max_attempts=5,
            backoff_base_seconds=1.0,
            backoff_max_seconds=4.0,
        )
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert runner.slept == [1.0, 2.0, 4.0, 4.0]  # type: ignore[attr-defined]

    async def test_a_definitive_miss_is_not_retried(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # Looking again will not make the site have the file.
        provider = FakeProvider(fail_with={"unknown:dune": ProviderNotFoundError("no")})
        runner = make_runner(provider, jobs, artifacts, clock, max_attempts=5)
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert provider.calls == ["unknown:dune"]

    async def test_an_unavailable_provider_is_retried(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(
            fail_with={"unknown:dune": ProviderUnavailableError("browser died")}
        )
        runner = make_runner(provider, jobs, artifacts, clock, max_attempts=2)
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert len(provider.calls) == 2


class TestTimeouts:
    async def test_an_item_that_hangs_is_timed_out(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(hang_keys=frozenset({"unknown:dune"}))
        runner = make_runner(
            provider, jobs, artifacts, clock, download_timeout_seconds=0.01, max_attempts=1
        )
        job = await make_job(jobs, clock, "Dune")

        await runner.run(job)

        assert job.items[0].error is not None
        assert job.items[0].error.code == ProviderTimeoutError.code

    async def test_a_hanging_item_does_not_block_the_others(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(hang_keys=frozenset({"unknown:dune"}))
        runner = make_runner(
            provider,
            jobs,
            artifacts,
            clock,
            download_timeout_seconds=0.01,
            max_attempts=1,
            download_concurrency=4,
        )
        job = await make_job(jobs, clock, "Dune", "Arrival")

        await runner.run(job)

        assert job.status is JobStatus.PARTIAL
        assert job.items[1].status is ItemStatus.SUCCEEDED


class TestCancellation:
    async def test_submitting_then_cancelling_stops_the_work(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(hang_keys=frozenset({"unknown:dune"}))
        runner = make_runner(provider, jobs, artifacts, clock, download_timeout_seconds=30)
        job = await make_job(jobs, clock, "Dune")

        await runner.submit(job)
        await asyncio.sleep(0)
        await runner.cancel(job)

        assert job.status is JobStatus.CANCELLED

    async def test_cancelling_an_already_finished_job_leaves_its_outcome_alone(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        # A cancel can arrive just after the last item lands. The result stands.
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")
        await runner.run(job)

        await runner.cancel(job)

        assert job.status is JobStatus.SUCCEEDED

    async def test_cancelling_a_job_that_was_never_submitted_still_cancels_it(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")

        await runner.cancel(job)

        assert job.status is JobStatus.CANCELLED

    async def test_closing_the_runner_stops_everything_in_flight(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(hang_keys=frozenset({"unknown:dune"}))
        runner = make_runner(provider, jobs, artifacts, clock, download_timeout_seconds=30)
        job = await make_job(jobs, clock, "Dune")
        await runner.submit(job)
        await asyncio.sleep(0)

        await runner.aclose()

        assert provider.closed is True

    async def test_closing_twice_is_harmless(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)

        await runner.aclose()
        await runner.aclose()

    async def test_draining_an_idle_runner_returns_at_once(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)

        assert await runner.drain(timeout=0.01) is True

    async def test_draining_reports_work_that_would_not_finish(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        provider = FakeProvider(hang_keys=frozenset({"unknown:dune"}))
        runner = make_runner(provider, jobs, artifacts, clock, download_timeout_seconds=30)
        job = await make_job(jobs, clock, "Dune")
        await runner.submit(job)

        assert await runner.drain(timeout=0.01) is False

        await runner.aclose()

    async def test_closing_gives_in_flight_work_a_chance_to_finish(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")
        await runner.submit(job)

        await runner.aclose(drain_timeout=5)

        assert job.status is JobStatus.SUCCEEDED


class TestBackoffJitter:
    def test_jitter_stays_within_the_delay_it_was_given(self) -> None:
        # Never longer than the computed delay, never zero -- retries spread out
        # without any of them collapsing into an immediate retry.
        for delay in (0.5, 1.0, 8.0):
            for _ in range(50):
                jittered = _default_jitter(delay)
                assert 0 < jittered <= delay


class TestSubmission:
    async def test_a_submitted_job_runs_to_completion(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")

        await runner.submit(job)
        settled = await jobs.wait_for_terminal(job.job_id, timeout=5, account=ALICE)

        assert settled.status is JobStatus.SUCCEEDED

    async def test_finished_tasks_are_not_retained(
        self, jobs: InMemoryJobStore, artifacts: LocalArtifactStore, clock: FakeClock
    ) -> None:
        runner = make_runner(FakeProvider(), jobs, artifacts, clock)
        job = await make_job(jobs, clock, "Dune")

        await runner.submit(job)
        await jobs.wait_for_terminal(job.job_id, timeout=5, account=ALICE)

        assert await runner.drain(timeout=5) is True
        assert runner.in_flight == 0
