"""The browser download provider, driven against a fake page."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from media_tool.core.keyring.credentials import FormSecrets
from media_tool.domain.media import MediaQuery
from media_tool.providers.base import (
    DownloadProvider,
    ProviderCredentialMissingError,
    ProviderError,
    ProviderNotFoundError,
    ProviderTimeoutError,
)
from media_tool.providers.browser.download_provider import BrowserDownloadProvider
from media_tool.providers.browser.recipes import SiteRecipe
from media_tool.storage.local import LocalArtifactStore
from tests.fakes.accounts import ALICE
from tests.fakes.browser import DOWNLOAD_PAYLOAD, FakeDownload, FakePage
from tests.fakes.clock import FakeClock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from media_tool.domain.artifacts import DownloadArtifact

RECIPE = SiteRecipe.model_validate(
    {
        "name": "example",
        "start_url": "https://example.test/search?q={query}",
        "steps": [
            {"action": "fill", "selector": "#q", "value": "{query}"},
            {"action": "click", "selector": "#search"},
            {"action": "wait_for", "selector": ".result"},
        ],
        "result_selector": ".result",
        "match": {"text_contains": ["{name}", "{episode_tag}"]},
        "download_trigger": {"action": "click", "selector": ".result a.download"},
    }
)

SEVERANCE = MediaQuery.create(name="Severance", season=1, episode=3)
DUNE = MediaQuery.create(name="Dune", year=2021)


class FakeRuntime:
    """Hands out one prepared page and records its own lifecycle."""

    def __init__(self, page: FakePage, *, healthy: bool = True) -> None:
        self.page = page
        self.closed = False
        self._healthy = healthy

    @asynccontextmanager
    async def acquire_page(self) -> AsyncIterator[FakePage]:
        yield self.page

    async def healthy(self) -> bool:
        return self._healthy

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(
        root=tmp_path / "artifacts", clock=FakeClock(), max_file_bytes=1_000_000
    )


def matching_page(**overrides: object) -> FakePage:
    defaults: dict[str, object] = {
        "texts": {".result": ["Severance S01E03 1080p"]},
        "download": FakeDownload(
            suggested_filename="severance.s01e03.mkv",
            url="https://cdn.example.test/severance.mkv",
            payload=DOWNLOAD_PAYLOAD,
        ),
    }
    defaults.update(overrides)
    return FakePage(**defaults)  # type: ignore[arg-type]


async def run(
    page: FakePage,
    store: LocalArtifactStore,
    query: MediaQuery = SEVERANCE,
    recipe: SiteRecipe = RECIPE,
    secrets: FormSecrets | None = None,
) -> DownloadArtifact:
    provider = BrowserDownloadProvider(runtime=FakeRuntime(page), recipe=recipe)
    with store.reserve(account=ALICE, job_id="job1", index=0) as sink:
        return await provider.download(query=query, sink=sink, secrets=secrets)


class TestPortConformance:
    def test_it_satisfies_the_download_provider_port(self) -> None:
        checked: DownloadProvider = BrowserDownloadProvider(
            runtime=FakeRuntime(matching_page()), recipe=RECIPE
        )

        assert checked is not None

    def test_it_names_the_recipe_it_is_driving(self) -> None:
        provider = BrowserDownloadProvider(runtime=FakeRuntime(matching_page()), recipe=RECIPE)

        assert provider.name == "browser:example"

    async def test_health_reflects_the_runtime(self) -> None:
        runtime = FakeRuntime(matching_page(), healthy=False)
        provider = BrowserDownloadProvider(runtime=runtime, recipe=RECIPE)

        assert await provider.healthy() is False

    async def test_closing_closes_the_runtime(self) -> None:
        runtime = FakeRuntime(matching_page())
        provider = BrowserDownloadProvider(runtime=runtime, recipe=RECIPE)

        await provider.aclose()

        assert runtime.closed is True


class TestTheFlow:
    async def test_it_captures_the_file(self, store: LocalArtifactStore) -> None:
        artifact = await run(matching_page(), store)

        located = store.locate(account=ALICE, job_id="job1", index=0, filename=artifact.filename)
        assert located.read_bytes() == DOWNLOAD_PAYLOAD

    async def test_it_records_where_the_file_came_from(self, store: LocalArtifactStore) -> None:
        artifact = await run(matching_page(), store)

        assert artifact.source_url == "https://cdn.example.test/severance.mkv"

    async def test_it_sanitizes_the_name_the_site_supplied(self, store: LocalArtifactStore) -> None:
        page = matching_page(
            download=FakeDownload(
                suggested_filename="../../../etc/passwd",
                url="https://cdn.example.test/x",
                payload=DOWNLOAD_PAYLOAD,
            )
        )

        artifact = await run(page, store)

        assert artifact.filename == "passwd"
        assert (
            store.root
            in store.locate(account=ALICE, job_id="job1", index=0, filename="passwd").parents
        )

    async def test_it_performs_every_step_in_order(self, store: LocalArtifactStore) -> None:
        page = matching_page()

        await run(page, store)

        assert page.actions == [
            ("goto", "https://example.test/search?q=Severance%20S01E03"),
            ("fill", "#q=Severance S01E03"),
            ("click", "#search"),
            ("wait_for", ".result"),
            ("click", ".result a.download"),
        ]

    async def test_the_start_url_is_encoded(self, store: LocalArtifactStore) -> None:
        page = matching_page()

        await run(page, store)

        assert page.actions[0] == ("goto", "https://example.test/search?q=Severance%20S01E03")

    async def test_a_recipe_without_steps_goes_straight_to_the_trigger(
        self, store: LocalArtifactStore
    ) -> None:
        recipe = SiteRecipe.model_validate(
            {
                "name": "direct",
                "start_url": "https://example.test/{name}",
                "download_trigger": {"action": "click", "selector": "a.download"},
            }
        )
        page = matching_page()

        await run(page, store, DUNE, recipe)

        assert page.actions == [("goto", "https://example.test/Dune"), ("click", "a.download")]


class TestMatching:
    async def test_a_page_with_no_results_is_a_miss(self, store: LocalArtifactStore) -> None:
        page = matching_page(texts={".result": []})

        with pytest.raises(ProviderNotFoundError, match="no results"):
            await run(page, store)

    async def test_a_page_whose_results_do_not_match_is_a_miss(
        self, store: LocalArtifactStore
    ) -> None:
        # Otherwise the trigger would click the first thing on the page and the job
        # would report success for the wrong file.
        page = matching_page(texts={".result": ["Severance S02E01", "Succession S01E03"]})

        with pytest.raises(ProviderNotFoundError, match="no result matched"):
            await run(page, store)

    async def test_matching_ignores_case(self, store: LocalArtifactStore) -> None:
        page = matching_page(texts={".result": ["severance s01e03 web-dl"]})

        assert (await run(page, store)).filename == "severance.s01e03.mkv"

    async def test_one_matching_result_among_others_is_enough(
        self, store: LocalArtifactStore
    ) -> None:
        page = matching_page(texts={".result": ["Succession S01E03", "Severance S01E03", "Other"]})

        assert (await run(page, store)).filename == "severance.s01e03.mkv"

    async def test_an_empty_placeholder_does_not_constrain(self, store: LocalArtifactStore) -> None:
        # '{episode_tag}' is empty for a film, so it must not be required.
        page = matching_page(texts={".result": ["Dune 2021 2160p"]})

        assert (await run(page, store, DUNE)).filename == "severance.s01e03.mkv"

    async def test_a_recipe_without_a_result_selector_skips_matching(
        self, store: LocalArtifactStore
    ) -> None:
        recipe = SiteRecipe.model_validate(
            {
                "name": "direct",
                "start_url": "https://example.test/{name}",
                "download_trigger": {"action": "click", "selector": "a.download"},
            }
        )
        page = matching_page(texts={})

        assert (await run(page, store, SEVERANCE, recipe)).filename == "severance.s01e03.mkv"

    async def test_a_recipe_with_results_but_no_match_rule_accepts_any(
        self, store: LocalArtifactStore
    ) -> None:
        recipe = SiteRecipe.model_validate(
            {
                "name": "loose",
                "start_url": "https://example.test/{name}",
                "result_selector": ".result",
                "download_trigger": {"action": "click", "selector": "a.download"},
            }
        )
        page = matching_page(texts={".result": ["anything at all"]})

        assert (await run(page, store, SEVERANCE, recipe)).filename == "severance.s01e03.mkv"


class TestFailures:
    async def test_navigation_failure_is_reported(self, store: LocalArtifactStore) -> None:
        page = matching_page(
            fail_on={"https://example.test/search?q=Severance%20S01E03": RuntimeError("dns")}
        )

        with pytest.raises(ProviderError, match="could not open"):
            await run(page, store)

    async def test_a_failing_step_names_itself(self, store: LocalArtifactStore) -> None:
        page = matching_page(fail_on={"#search": RuntimeError("element detached")})

        with pytest.raises(ProviderError, match=re.escape("click on '#search' failed")):
            await run(page, store)

    async def test_a_missing_element_names_itself(self, store: LocalArtifactStore) -> None:
        page = matching_page(fail_on={".result": RuntimeError("timeout waiting")})

        with pytest.raises(ProviderError, match=re.escape("wait_for on '.result' failed")):
            await run(page, store)

    async def test_a_click_that_starts_no_download_times_out(
        self, store: LocalArtifactStore
    ) -> None:
        page = matching_page(never_downloads=True)

        with pytest.raises(ProviderTimeoutError, match="started no download"):
            await run(page, store)

    async def test_a_failing_trigger_is_reported_as_a_step_failure(
        self, store: LocalArtifactStore
    ) -> None:
        page = matching_page(fail_on={".result a.download": RuntimeError("not clickable")})

        with pytest.raises(ProviderError, match=re.escape("a.download' failed")):
            await run(page, store)

    async def test_a_browser_crash_mid_capture_is_wrapped(self, store: LocalArtifactStore) -> None:
        # The context can die while the download is in flight; the caller should get a
        # download failure naming the trigger, not a raw driver exception.
        page = matching_page(capture_error=RuntimeError("target page closed"))

        with pytest.raises(ProviderError, match="capturing the download"):
            await run(page, store)

    async def test_a_failure_saving_the_bytes_is_reported_as_a_download_failure(
        self, store: LocalArtifactStore
    ) -> None:
        # Must not escape unclassified, or the runner logs it as an internal bug.
        page = matching_page()
        page._download = _ExplodingDownload()  # type: ignore[assignment]

        with pytest.raises(ProviderError, match=re.escape("could not save 'x.bin'")):
            await run(page, store)


class _ExplodingDownload:
    """A download whose bytes cannot be saved."""

    suggested_filename = "x.bin"
    url = "https://x.test"

    async def save_as(self, path: object) -> None:  # noqa: ARG002 - mirrors the real signature
        msg = "disk went away"
        raise OSError(msg)


LOGIN_RECIPE = SiteRecipe.model_validate(
    {
        "name": "walled-garden",
        "start_url": "https://example.test/search?q={query}",
        "steps": [
            {"action": "fill", "selector": "#user", "value": "{secret.username}"},
            {"action": "fill", "selector": "#pass", "value": "{secret.password}"},
            {"action": "click", "selector": "button[type=submit]"},
        ],
        "result_selector": ".result",
        "download_trigger": {"action": "click", "selector": "a.download"},
        "login": {"service": "somesite"},
    }
)

SECRETS = FormSecrets(service="somesite", fields={"username": "eve", "password": "hunter2"})


class TestLoggingIn:
    def test_it_declares_the_login_its_recipe_needs(self) -> None:
        # Declared rather than discovered, so the caller can resolve the credential
        # before any browser starts.
        provider = BrowserDownloadProvider(
            runtime=FakeRuntime(matching_page()), recipe=LOGIN_RECIPE
        )

        assert provider.requires_login == "somesite"

    def test_a_recipe_without_a_login_declares_none(self) -> None:
        provider = BrowserDownloadProvider(runtime=FakeRuntime(matching_page()), recipe=RECIPE)

        assert provider.requires_login is None

    async def test_the_login_values_are_typed_into_the_form(
        self, store: LocalArtifactStore
    ) -> None:
        page = matching_page()

        await run(page, store, recipe=LOGIN_RECIPE, secrets=SECRETS)

        assert ("fill", "#user=eve") in page.actions
        assert ("fill", "#pass=hunter2") in page.actions

    async def test_no_login_value_reaches_the_url(self, store: LocalArtifactStore) -> None:
        # The recipe model forbids it and this proves the rendering agrees: a password in
        # a URL is a password in an access log.
        page = matching_page()

        await run(page, store, recipe=LOGIN_RECIPE, secrets=SECRETS)

        visited = [target for kind, target in page.actions if kind == "goto"]

        assert visited
        assert all("hunter2" not in url for url in visited)

    async def test_a_missing_field_fails_before_the_browser_is_touched(
        self, store: LocalArtifactStore
    ) -> None:
        # The whole point of resolving up front: a recipe asking for a field the stored
        # login does not have should not first drive a browser to a login form.
        page = matching_page()
        thin = FormSecrets(service="somesite", fields={"username": "eve"})

        with pytest.raises(ProviderCredentialMissingError, match="password"):
            await run(page, store, recipe=LOGIN_RECIPE, secrets=thin)

        assert page.actions == []

    async def test_a_recipe_needing_a_login_refuses_to_run_without_one(
        self, store: LocalArtifactStore
    ) -> None:
        page = matching_page()

        with pytest.raises(ProviderCredentialMissingError, match="somesite"):
            await run(page, store, recipe=LOGIN_RECIPE, secrets=None)

        assert page.actions == []

    async def test_a_step_failure_does_not_report_what_was_typed(
        self, store: LocalArtifactStore
    ) -> None:
        # A failing fill step is the most likely place for a password to reach a log, so
        # the message names the selector and never the value.
        page = matching_page(fail_on={"#pass": RuntimeError("element detached")})

        with pytest.raises(ProviderError) as caught:
            await run(page, store, recipe=LOGIN_RECIPE, secrets=SECRETS)

        assert "#pass" in str(caught.value)
        assert "hunter2" not in str(caught.value)
