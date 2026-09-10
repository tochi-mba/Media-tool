"""Headless Chromium, driven by Playwright.

Playwright is imported lazily, inside the method that needs it, through an injectable
factory. That is what lets the rest of the package import cleanly when the ``browser``
extra is not installed, and it is what makes the missing-dependency path testable.

One browser process is shared; each acquisition gets its own context, so cookies and
downloads from one item cannot bleed into another. Contexts accept downloads, which is
the whole point: the click is automated and the file is captured without a save dialog.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from media_tool.core.logging import get_logger
from media_tool.providers.base import ProviderUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from media_tool.core.config import BrowserSettings
    from media_tool.providers.browser.page import PageLike

logger = get_logger(__name__)

PLAYWRIGHT_MISSING_MESSAGE = (
    "the browser provider needs Playwright: install the 'browser' extra "
    "(uv sync --all-extras) and make a Chromium build available"
)


def _import_playwright() -> Any:
    """Import the async Playwright entry point on demand.

    Raises:
        ProviderUnavailableError: if the optional dependency is not installed.
    """
    try:
        # Deliberately local: this import is the thing being made optional, so it must
        # not run at module import time.
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError as error:
        raise ProviderUnavailableError(PLAYWRIGHT_MISSING_MESSAGE) from error

    return async_playwright


class _PlaywrightPage:
    """Adapts a Playwright page to the narrow :class:`PageLike` port."""

    def __init__(self, page: Any, settings: BrowserSettings) -> None:
        self._page = page
        self._settings = settings

    async def goto(self, url: str, *, timeout: float | None = None) -> None:
        await self._page.goto(url, timeout=timeout or self._settings.navigation_timeout_ms)

    async def click(self, selector: str, *, timeout: float | None = None) -> None:
        await self._page.click(selector, timeout=timeout or self._settings.action_timeout_ms)

    async def fill(self, selector: str, value: str, *, timeout: float | None = None) -> None:
        await self._page.fill(selector, value, timeout=timeout or self._settings.action_timeout_ms)

    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:
        await self._page.wait_for_selector(
            selector, timeout=timeout or self._settings.action_timeout_ms
        )

    async def text_contents(self, selector: str) -> list[str]:
        texts: list[str] = await self._page.locator(selector).all_text_contents()
        return texts

    async def query_count(self, selector: str) -> int:
        count: int = await self._page.locator(selector).count()
        return count

    def expect_download(self, *, timeout: float | None = None) -> Any:
        return self._page.expect_download(timeout=timeout or self._settings.download_timeout_ms)


class PlaywrightBrowserRuntime:
    """Owns a headless Chromium process and hands out pages."""

    def __init__(
        self,
        settings: BrowserSettings,
        *,
        downloads_dir: Path,
        playwright_factory: Callable[[], Any] = _import_playwright,
    ) -> None:
        self._settings = settings
        self._downloads_dir = downloads_dir
        self._playwright_factory = playwright_factory
        self._pages = asyncio.Semaphore(settings.max_pages)
        self._lock = asyncio.Lock()
        self._driver: Any = None
        self._browser: Any = None

    async def healthy(self) -> bool:
        """Whether Playwright is importable, and the browser has not crashed if started."""
        try:
            self._playwright_factory()
        except ProviderUnavailableError:
            return False

        if self._browser is None:
            return True
        connected: bool = self._browser.is_connected()
        return connected

    @asynccontextmanager
    async def acquire_page(self) -> AsyncIterator[PageLike]:
        """Check out a page from a fresh context, tearing both down on the way out."""
        async with self._pages:
            browser = await self._ensure_browser()
            context = await browser.new_context(**self._context_options())
            try:
                page = await context.new_page()
                yield _PlaywrightPage(page, self._settings)
            finally:
                # Closing the context closes its pages, and does so even if the body
                # raised, so a failed download never strands a browser tab.
                await context.close()

    async def aclose(self) -> None:
        """Stop the browser and the driver. Safe to call twice.

        State is cleared under the lock and the slow shutdown happens outside it, so a
        second caller returns immediately rather than queueing behind a browser that is
        already on its way down.
        """
        async with self._lock:
            browser, self._browser = self._browser, None
            driver, self._driver = self._driver, None

        if browser is not None:
            await browser.close()
        if driver is not None:
            await driver.stop()

    async def _ensure_browser(self) -> Any:
        """Start the browser once, on first use."""
        async with self._lock:
            if self._browser is not None:
                return self._browser

            async_playwright = self._playwright_factory()
            self._driver = await async_playwright().start()

            launch_options: dict[str, Any] = {
                "headless": self._settings.headless,
                "args": list(self._settings.args),
            }
            if self._settings.executable_path is not None:
                # Also bypasses Playwright's browser-revision check, which matters when
                # the installed Playwright is newer than the available Chromium build.
                launch_options["executable_path"] = str(self._settings.executable_path)

            self._browser = await self._driver.chromium.launch(**launch_options)
            logger.info("browser_started", headless=self._settings.headless)
            return self._browser

    def _context_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "accept_downloads": True,
            "downloads_path": str(self._downloads_dir),
        }
        if self._settings.user_agent is not None:
            options["user_agent"] = self._settings.user_agent
        if self._settings.storage_state_path is not None:
            # A saved session, so a site that needs a login does not need one every run.
            options["storage_state"] = str(self._settings.storage_state_path)
        return options


__all__ = ["PLAYWRIGHT_MISSING_MESSAGE", "PlaywrightBrowserRuntime"]
