"""The composition root, including background retention."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from media_tool.core.config import Settings
from media_tool.core.container import Container
from media_tool.domain.jobs import Job
from media_tool.domain.media import MediaQuery
from media_tool.providers.stub import StubDownloadProvider
from tests.fakes.accounts import ALICE
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def container(tmp_path: Path, clock: FakeClock) -> Container:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        require_authentication=False,
        artifact_dir=tmp_path / "artifacts",
        job_ttl_seconds=60,
        artifact_ttl_seconds=60,
        job_sweep_interval_seconds=0.01,
    )
    return Container.build(settings, clock=clock)


class TestWiring:
    def test_it_builds_every_adapter(self, container: Container) -> None:
        assert isinstance(container.provider, StubDownloadProvider)
        assert container.artifacts.root.exists()
        assert container.runner is not None

    def test_uptime_advances_with_the_clock(self, container: Container, clock: FakeClock) -> None:
        clock.advance(5)

        assert container.uptime_seconds == pytest.approx(5)


class TestRetention:
    async def test_sweeping_drops_expired_jobs_and_their_files(
        self, container: Container, clock: FakeClock
    ) -> None:
        job = Job.create(account=ALICE, queries=[MediaQuery.create(name="Dune")], now=clock.now())
        await container.jobs.add(job)
        await container.runner.run(job)
        assert job.items[0].artifact is not None
        filename = job.items[0].artifact.filename

        clock.advance(timedelta(seconds=61))
        purged = await container.sweep_once()

        assert purged == 1
        assert not (container.artifacts.root / job.job_id).exists()
        assert filename  # the file existed before the sweep removed it

    async def test_sweeping_leaves_live_jobs_alone(
        self, container: Container, clock: FakeClock
    ) -> None:
        job = Job.create(account=ALICE, queries=[MediaQuery.create(name="Dune")], now=clock.now())
        await container.jobs.add(job)

        assert await container.sweep_once() == 0
        assert await container.jobs.count() == 1


class TestBackgroundSweeper:
    async def test_the_sweeper_evicts_on_its_own(
        self, container: Container, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = Job.create(account=ALICE, queries=[MediaQuery.create(name="Dune")], now=clock.now())
        await container.jobs.add(job)
        clock.advance(timedelta(seconds=61))

        swept = asyncio.Event()
        real_purge = container.jobs.purge_expired

        async def purge_and_signal(*, ttl_seconds: float) -> list[str]:
            purged = await real_purge(ttl_seconds=ttl_seconds)
            swept.set()
            return purged

        monkeypatch.setattr(container.jobs, "purge_expired", purge_and_signal)
        container.start_sweeper()

        async with asyncio.timeout(5):
            await swept.wait()

        assert await container.jobs.count() == 0
        await container.aclose()

    async def test_a_failing_sweep_does_not_kill_the_sweeper(
        self, container: Container, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One bad tick must not silently end retention for the life of the process.
        tried_twice = asyncio.Event()
        calls = 0

        async def explode(**_kwargs: object) -> list[str]:
            nonlocal calls
            calls += 1
            if calls >= 2:
                tried_twice.set()
            msg = "store is unhappy"
            raise RuntimeError(msg)

        monkeypatch.setattr(container.jobs, "purge_expired", explode)
        container.start_sweeper()

        async with asyncio.timeout(5):
            await tried_twice.wait()

        await container.aclose()

    async def test_closing_stops_the_sweeper(self, container: Container) -> None:
        container.start_sweeper()

        await container.aclose()

        assert container._sweeper is None

    async def test_closing_without_a_sweeper_is_harmless(self, container: Container) -> None:
        await container.aclose()
