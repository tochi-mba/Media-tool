"""The stub provider.

It exists so the whole pipeline -- job, runner, storage, HTTP -- can be exercised
without a browser. It therefore has to behave like a real provider: produce a real file,
report a real source, and fail in the same ways.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from media_tool.domain.media import MediaQuery
from media_tool.providers.base import DownloadProvider, ProviderNotFoundError
from media_tool.providers.stub import StubDownloadProvider
from media_tool.storage.local import LocalArtifactStore
from tests.fakes.accounts import ALICE
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from pathlib import Path

    from media_tool.domain.artifacts import DownloadArtifact


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(
        root=tmp_path / "artifacts", clock=FakeClock(), max_file_bytes=1_000_000
    )


@pytest.fixture
def provider() -> StubDownloadProvider:
    return StubDownloadProvider()


async def download(
    provider: StubDownloadProvider, store: LocalArtifactStore, query: MediaQuery
) -> DownloadArtifact:
    with store.reserve(account=ALICE, job_id="job1", index=0) as sink:
        return await provider.download(query=query, sink=sink)


class TestPortConformance:
    def test_the_stub_satisfies_the_port(self, provider: StubDownloadProvider) -> None:
        checked: DownloadProvider = provider

        assert checked is provider

    def test_it_reports_its_name(self, provider: StubDownloadProvider) -> None:
        assert provider.name == "stub"

    async def test_it_is_always_ready(self, provider: StubDownloadProvider) -> None:
        assert await provider.healthy() is True


class TestDownloading:
    async def test_it_produces_a_real_file(
        self, provider: StubDownloadProvider, store: LocalArtifactStore
    ) -> None:
        query = MediaQuery.create(name="Severance", season=1, episode=3)

        artifact = await download(provider, store, query)

        located = store.locate(account=ALICE, job_id="job1", index=0, filename=artifact.filename)
        assert located.stat().st_size == artifact.size_bytes
        assert artifact.size_bytes > 0

    async def test_the_filename_describes_what_was_asked_for(
        self, provider: StubDownloadProvider, store: LocalArtifactStore
    ) -> None:
        query = MediaQuery.create(name="Severance", season=1, episode=3)

        artifact = await download(provider, store, query)

        assert artifact.filename == "severance-s01e03.stub.bin"

    async def test_a_film_filename_uses_the_year(
        self, provider: StubDownloadProvider, store: LocalArtifactStore
    ) -> None:
        artifact = await download(provider, store, MediaQuery.create(name="Dune", year=2021))

        assert artifact.filename == "dune-2021.stub.bin"

    async def test_a_bare_name_still_produces_a_filename(
        self, provider: StubDownloadProvider, store: LocalArtifactStore
    ) -> None:
        artifact = await download(provider, store, MediaQuery.create(name="Dune"))

        assert artifact.filename == "dune.stub.bin"

    async def test_the_source_url_identifies_the_stub(
        self, provider: StubDownloadProvider, store: LocalArtifactStore
    ) -> None:
        artifact = await download(provider, store, MediaQuery.create(name="Dune"))

        assert artifact.source_url.startswith("stub://")

    async def test_the_content_is_deterministic_for_a_given_query(
        self, provider: StubDownloadProvider, store: LocalArtifactStore, tmp_path: Path
    ) -> None:
        other = LocalArtifactStore(
            root=tmp_path / "other", clock=FakeClock(), max_file_bytes=1_000_000
        )
        query = MediaQuery.create(name="Dune", year=2021)

        first = await download(provider, store, query)
        second = await download(provider, other, query)

        assert first.sha256 == second.sha256

    async def test_different_queries_produce_different_content(
        self, provider: StubDownloadProvider, store: LocalArtifactStore, tmp_path: Path
    ) -> None:
        other = LocalArtifactStore(
            root=tmp_path / "other", clock=FakeClock(), max_file_bytes=1_000_000
        )

        first = await download(provider, store, MediaQuery.create(name="Dune", year=2021))
        second = await download(provider, other, MediaQuery.create(name="Arrival", year=2016))

        assert first.sha256 != second.sha256


class TestConfiguredFailures:
    """The stub can be told to fail, so failure paths downstream are testable."""

    async def test_a_configured_miss_raises_not_found(self, store: LocalArtifactStore) -> None:
        provider = StubDownloadProvider(missing={"movie:dune:2021"})

        with pytest.raises(ProviderNotFoundError, match="Dune"):
            await download(provider, store, MediaQuery.create(name="Dune", year=2021))

    async def test_other_queries_still_succeed(self, store: LocalArtifactStore) -> None:
        provider = StubDownloadProvider(missing={"movie:dune:2021"})

        artifact = await download(provider, store, MediaQuery.create(name="Arrival", year=2016))

        assert artifact.filename == "arrival-2016.stub.bin"
