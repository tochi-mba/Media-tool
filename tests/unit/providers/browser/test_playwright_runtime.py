"""The Playwright runtime, driven by a fake driver.

Every line here is also exercised against real Chromium in tests/live; this covers the
same paths deterministically and without a browser process.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from media_tool.core.config import BrowserSettings
from media_tool.providers.base import ProviderUnavailableError
from media_tool.providers.browser.page import BrowserRuntime, PageLike
from media_tool.providers.browser.playwright_runtime import (
    PLAYWRIGHT_MISSING_MESSAGE,
    PlaywrightBrowserRuntime,
    _import_playwright,
)
from tests.fakes.browser import FakeNativePage, FakePlaywrightFactory

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def factory() -> FakePlaywrightFactory:
    return FakePlaywrightFactory()


def make_runtime(
    factory: FakePlaywrightFactory, tmp_path: Path, **overrides: object
) -> PlaywrightBrowserRuntime:
    return PlaywrightBrowserRuntime(
        BrowserSettings(**overrides),  # type: ignore[arg-type]
        downloads_dir=tmp_path / "downloads",
        playwright_factory=factory,
    )


class TestPortConformance:
    """The adapter must satisfy the narrow port, not the other way round."""

    def test_the_runtime_satisfies_the_browser_port(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        checked: BrowserRuntime = make_runtime(factory, tmp_path)

        assert checked is not None

    async def test_an_acquired_page_satisfies_the_page_port(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page() as page:
            checked: PageLike = page
            assert checked is page

        await runtime.aclose()


class TestLaunching:
    async def test_the_browser_starts_on_first_use(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)

        assert factory.driver.chromium.launches == 0

        async with runtime.acquire_page():
            assert factory.driver.chromium.launches == 1

        await runtime.aclose()

    async def test_the_browser_is_started_only_once(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page():
            pass
        async with runtime.acquire_page():
            pass

        assert factory.driver.chromium.launches == 1
        await runtime.aclose()

    async def test_it_launches_headless_by_default(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page():
            pass

        assert factory.driver.chromium.launch_options == {"headless": True, "args": []}
        await runtime.aclose()

    async def test_an_explicit_binary_is_passed_through(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        # Needed when the installed Playwright is newer than the available Chromium;
        # supplying a path also bypasses the revision check.
        runtime = make_runtime(factory, tmp_path, executable_path="/opt/pw-browsers/chromium")

        async with runtime.acquire_page():
            pass

        options = factory.driver.chromium.launch_options
        assert options is not None
        assert options["executable_path"] == "/opt/pw-browsers/chromium"
        await runtime.aclose()

    async def test_extra_launch_arguments_are_passed_through(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path, args=("--no-sandbox",))

        async with runtime.acquire_page():
            pass

        options = factory.driver.chromium.launch_options
        assert options is not None
        assert options["args"] == ["--no-sandbox"]
        await runtime.aclose()


class TestContexts:
    async def test_contexts_accept_downloads(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        # Without this the browser would prompt instead of saving, which is the whole
        # thing this service exists to avoid.
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page():
            pass

        assert factory.browser.contexts[0].options["accept_downloads"] is True
        await runtime.aclose()

    async def test_downloads_land_in_the_configured_directory(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page():
            pass

        options = factory.browser.contexts[0].options
        assert options["downloads_path"] == str(tmp_path / "downloads")
        await runtime.aclose()

    async def test_each_acquisition_gets_a_fresh_context(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        # So cookies and downloads from one item cannot bleed into another.
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page():
            pass
        async with runtime.acquire_page():
            pass

        assert len(factory.browser.contexts) == 2
        await runtime.aclose()

    async def test_the_context_is_closed_afterwards(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page():
            pass

        assert factory.browser.contexts[0].closed is True
        await runtime.aclose()

    async def test_the_context_is_closed_even_when_the_body_fails(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        # A failed download must not strand a browser tab.
        runtime = make_runtime(factory, tmp_path)

        with pytest.raises(RuntimeError):
            await _fail_inside(runtime)

        assert factory.browser.contexts[0].closed is True
        await runtime.aclose()

    async def test_a_saved_session_is_loaded_when_configured(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        state = tmp_path / "session.json"
        runtime = make_runtime(factory, tmp_path, storage_state_path=state)

        async with runtime.acquire_page():
            pass

        assert factory.browser.contexts[0].options["storage_state"] == str(state)
        await runtime.aclose()

    async def test_a_custom_user_agent_is_applied(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path, user_agent="MediaTool/1.0")

        async with runtime.acquire_page():
            pass

        assert factory.browser.contexts[0].options["user_agent"] == "MediaTool/1.0"
        await runtime.aclose()

    async def test_nothing_optional_is_sent_when_unconfigured(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)

        async with runtime.acquire_page():
            pass

        options = factory.browser.contexts[0].options
        assert "storage_state" not in options
        assert "user_agent" not in options
        await runtime.aclose()


class TestPageCap:
    async def test_pages_are_capped(self, factory: FakePlaywrightFactory, tmp_path: Path) -> None:
        # Pages cost real memory; a large batch waits rather than opening one each.
        runtime = make_runtime(factory, tmp_path, max_pages=2)
        concurrent = 0
        peak = 0

        async def hold() -> None:
            nonlocal concurrent, peak
            async with runtime.acquire_page():
                concurrent += 1
                peak = max(peak, concurrent)
                await asyncio.sleep(0)
                concurrent -= 1

        await asyncio.gather(*(hold() for _ in range(6)))

        assert peak <= 2
        await runtime.aclose()


class TestPageOperations:
    async def test_calls_are_forwarded_with_the_configured_timeouts(self, tmp_path: Path) -> None:
        native = FakeNativePage(texts={".result": ["Severance S01E03"]})
        factory = FakePlaywrightFactory(native)
        runtime = make_runtime(factory, tmp_path, navigation_timeout_ms=1234, action_timeout_ms=567)

        async with runtime.acquire_page() as page:
            await page.goto("https://example.test")
            await page.click("a.download")
            await page.fill("#q", "Severance")
            await page.wait_for_selector(".result")

            assert await page.text_contents(".result") == ["Severance S01E03"]
            assert await page.query_count(".result") == 1

        assert ("goto", "https://example.test", 1234) in native.calls
        assert ("click", "a.download", 567) in native.calls
        assert ("fill", ("#q", "Severance"), 567) in native.calls
        assert ("wait_for_selector", ".result", 567) in native.calls
        await runtime.aclose()

    async def test_an_explicit_timeout_wins(self, tmp_path: Path) -> None:
        native = FakeNativePage()
        factory = FakePlaywrightFactory(native)
        runtime = make_runtime(factory, tmp_path, navigation_timeout_ms=1234)

        async with runtime.acquire_page() as page:
            await page.goto("https://example.test", timeout=99)

        assert ("goto", "https://example.test", 99) in native.calls
        await runtime.aclose()

    async def test_download_capture_uses_the_download_timeout(self, tmp_path: Path) -> None:
        native = FakeNativePage()
        factory = FakePlaywrightFactory(native)
        runtime = make_runtime(factory, tmp_path, download_timeout_ms=45_000)

        async with runtime.acquire_page() as page:
            page.expect_download()

        assert ("expect_download", None, 45_000) in native.calls
        await runtime.aclose()


class TestShutdown:
    async def test_closing_stops_the_browser_and_the_driver(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)
        async with runtime.acquire_page():
            pass

        await runtime.aclose()

        assert factory.browser.closed is True
        assert factory.driver.stopped is True

    async def test_closing_twice_is_harmless(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)
        async with runtime.acquire_page():
            pass

        await runtime.aclose()
        await runtime.aclose()

    async def test_closing_a_runtime_that_never_started_is_harmless(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        await make_runtime(factory, tmp_path).aclose()

    async def test_the_browser_restarts_after_a_close(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)
        async with runtime.acquire_page():
            pass
        await runtime.aclose()

        factory.browser.connected = True
        async with runtime.acquire_page():
            pass

        assert factory.driver.chromium.launches == 2
        await runtime.aclose()


class TestHealth:
    async def test_an_unstarted_runtime_is_healthy(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        assert await make_runtime(factory, tmp_path).healthy() is True

    async def test_a_running_browser_is_healthy(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)
        async with runtime.acquire_page():
            pass

        assert await runtime.healthy() is True
        await runtime.aclose()

    async def test_a_crashed_browser_is_unhealthy(
        self, factory: FakePlaywrightFactory, tmp_path: Path
    ) -> None:
        runtime = make_runtime(factory, tmp_path)
        async with runtime.acquire_page():
            pass
        factory.browser.connected = False

        assert await runtime.healthy() is False
        await runtime.aclose()

    async def test_a_runtime_without_playwright_is_unhealthy(self, tmp_path: Path) -> None:
        def missing() -> object:
            raise ProviderUnavailableError(PLAYWRIGHT_MISSING_MESSAGE)

        runtime = PlaywrightBrowserRuntime(
            BrowserSettings(),
            downloads_dir=tmp_path,
            playwright_factory=missing,
        )

        assert await runtime.healthy() is False


class TestMissingDependency:
    async def test_acquiring_a_page_without_playwright_explains_how_to_fix_it(
        self, tmp_path: Path
    ) -> None:
        def missing() -> object:
            raise ProviderUnavailableError(PLAYWRIGHT_MISSING_MESSAGE)

        runtime = PlaywrightBrowserRuntime(
            BrowserSettings(), downloads_dir=tmp_path, playwright_factory=missing
        )

        with pytest.raises(ProviderUnavailableError, match="browser"):
            await _open_and_discard(runtime)

    def test_the_real_importer_reports_a_missing_playwright(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def deny(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("playwright"):
                msg = "No module named 'playwright'"
                raise ImportError(msg)
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", deny)

        with pytest.raises(ProviderUnavailableError, match="browser"):
            _import_playwright()

    def test_the_real_importer_returns_playwright_when_installed(self) -> None:
        assert callable(_import_playwright())


async def _fail_inside(runtime: PlaywrightBrowserRuntime) -> None:
    async with runtime.acquire_page():
        msg = "something went wrong mid-download"
        raise RuntimeError(msg)


async def _open_and_discard(runtime: PlaywrightBrowserRuntime) -> None:
    async with runtime.acquire_page():
        pass
