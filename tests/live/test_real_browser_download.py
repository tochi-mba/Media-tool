"""Real Chromium, really downloading a file, with nobody clicking anything.

Everything these exercise is also covered deterministically by the fake-driver unit
tests. What these add is proof that the real driver behaves the way the fake claims:
that a click inside `expect_download` genuinely captures a file, that
`accept_downloads` really suppresses the save dialog, and that a site's
Content-Disposition name reaches our sanitizer.

Deselected by default. Run with `make test-live`.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from media_tool.core.config import BrowserSettings
from media_tool.domain.media import MediaQuery
from media_tool.providers.base import ProviderNotFoundError
from media_tool.providers.browser.download_provider import BrowserDownloadProvider
from media_tool.providers.browser.playwright_runtime import PlaywrightBrowserRuntime
from media_tool.providers.browser.recipes import SiteRecipe
from media_tool.storage.local import LocalArtifactStore
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from media_tool.domain.artifacts import DownloadArtifact

pytestmark = pytest.mark.live_browser

SEVERANCE = MediaQuery.create(name="Severance", season=1, episode=3)
DUNE = MediaQuery.create(name="Dune", year=2021)


def recipe_for(site_url: str) -> SiteRecipe:
    """The shipped example recipe, pointed at the local fixture site."""
    return SiteRecipe.model_validate(
        {
            "name": "fixture",
            "start_url": f"{site_url}/search.html?q={{query}}",
            "steps": [{"action": "wait_for", "selector": ".result"}],
            "result_selector": ".result",
            "match": {"text_contains": ["{name}", "{episode_tag}"]},
            "download_trigger": {
                "action": "click",
                "selector": ".result:has-text('{name}') a.download",
            },
        }
    )


@pytest.fixture
async def provider(
    site_url: str, chromium_executable: str | None, tmp_path: Path
) -> AsyncIterator[BrowserDownloadProvider]:
    runtime = PlaywrightBrowserRuntime(
        BrowserSettings(
            headless=True,
            executable_path=chromium_executable,  # type: ignore[arg-type]
            navigation_timeout_ms=20_000,
            action_timeout_ms=10_000,
            download_timeout_ms=30_000,
        ),
        downloads_dir=tmp_path / "browser-downloads",
    )
    browser_provider = BrowserDownloadProvider(runtime=runtime, recipe=recipe_for(site_url))
    try:
        yield browser_provider
    finally:
        await browser_provider.aclose()


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(
        root=tmp_path / "artifacts", clock=FakeClock(), max_file_bytes=10_000_000
    )


async def fetch(
    provider: BrowserDownloadProvider,
    store: LocalArtifactStore,
    query: MediaQuery,
    index: int = 0,
) -> DownloadArtifact:
    with store.reserve(job_id="live", index=index) as sink:
        return await provider.download(query=query, sink=sink)


async def test_a_real_browser_downloads_an_episode_without_anyone_clicking(
    provider: BrowserDownloadProvider, store: LocalArtifactStore
) -> None:
    artifact = await fetch(provider, store, SEVERANCE)

    stored = store.locate(job_id="live", index=0, filename=artifact.filename)
    assert stored.read_bytes() == b"SEVERANCE-EPISODE-PAYLOAD" * 400


async def test_the_filename_comes_from_the_sites_content_disposition(
    provider: BrowserDownloadProvider, store: LocalArtifactStore
) -> None:
    artifact = await fetch(provider, store, SEVERANCE)

    assert artifact.filename == "severance.s01e03.bin"


async def test_the_recorded_digest_matches_the_bytes_on_disk(
    provider: BrowserDownloadProvider, store: LocalArtifactStore
) -> None:
    artifact = await fetch(provider, store, SEVERANCE)

    stored = store.locate(job_id="live", index=0, filename=artifact.filename)
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == artifact.sha256
    assert artifact.size_bytes == stored.stat().st_size


async def test_the_source_url_is_where_the_bytes_came_from(
    provider: BrowserDownloadProvider, store: LocalArtifactStore, site_url: str
) -> None:
    artifact = await fetch(provider, store, SEVERANCE)

    assert artifact.source_url == f"{site_url}/files/severance.s01e03.bin"


async def test_a_film_resolves_to_a_different_file(
    provider: BrowserDownloadProvider, store: LocalArtifactStore
) -> None:
    artifact = await fetch(provider, store, DUNE, index=1)

    stored = store.locate(job_id="live", index=1, filename=artifact.filename)
    assert stored.read_bytes() == b"DUNE-FILM-PAYLOAD" * 400


async def test_a_title_the_site_does_not_have_is_a_clean_miss(
    provider: BrowserDownloadProvider, store: LocalArtifactStore
) -> None:
    # Rather than clicking whatever happens to be first and reporting success for the
    # wrong file.
    with pytest.raises(ProviderNotFoundError):
        await fetch(provider, store, MediaQuery.create(name="Nonexistent Show", season=9))


async def test_the_browser_survives_several_downloads(
    provider: BrowserDownloadProvider, store: LocalArtifactStore
) -> None:
    first = await fetch(provider, store, SEVERANCE, index=0)
    second = await fetch(provider, store, DUNE, index=1)

    assert first.sha256 != second.sha256
    assert await provider.healthy() is True
