"""A fake Playwright driver.

Deep enough to exercise every line of the real adapter -- launch, contexts, pages,
downloads, teardown -- without starting a browser process. The live tests in
``tests/live`` cover the same paths against real Chromium; this covers them
deterministically and in milliseconds.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

DOWNLOAD_PAYLOAD = b"fake browser download"


class FakeDownload:
    """A file the fake browser 'downloaded'."""

    def __init__(self, *, suggested_filename: str, url: str, payload: bytes) -> None:
        self.suggested_filename = suggested_filename
        self.url = url
        self._payload = payload
        self.saved_to: Path | None = None

    async def save_as(self, path: str | Path) -> None:
        from pathlib import Path as _Path

        destination = _Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._payload)
        self.saved_to = destination


class FakeDownloadHandle:
    """Mirrors Playwright's ``expect_download`` handle."""

    def __init__(self, download: FakeDownload) -> None:
        self._download = download

    @property
    async def value(self) -> FakeDownload:
        return self._download


class FakePage:
    """Records what was done to it and answers however the test configured it."""

    def __init__(
        self,
        *,
        texts: dict[str, list[str]] | None = None,
        download: FakeDownload | None = None,
        fail_on: dict[str, Exception] | None = None,
        never_downloads: bool = False,
        capture_error: Exception | None = None,
    ) -> None:
        self._texts = texts or {}
        self._download = download
        self._fail_on = fail_on or {}
        self._never_downloads = never_downloads
        self._capture_error = capture_error

        self.actions: list[tuple[str, str]] = []
        self.closed = False
        self.goto_timeouts: list[float | None] = []

    def _maybe_fail(self, selector: str) -> None:
        if (error := self._fail_on.get(selector)) is not None:
            raise error

    async def goto(self, url: str, *, timeout: float | None = None) -> None:
        self._maybe_fail(url)
        self.goto_timeouts.append(timeout)
        self.actions.append(("goto", url))

    async def click(self, selector: str, *, timeout: float | None = None) -> None:
        self._maybe_fail(selector)
        self.actions.append(("click", selector))

    async def fill(self, selector: str, value: str, *, timeout: float | None = None) -> None:
        self._maybe_fail(selector)
        self.actions.append(("fill", f"{selector}={value}"))

    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:
        self._maybe_fail(selector)
        self.actions.append(("wait_for", selector))

    async def text_contents(self, selector: str) -> list[str]:
        return self._texts.get(selector, [])

    async def query_count(self, selector: str) -> int:
        return len(self._texts.get(selector, []))

    @asynccontextmanager
    async def expect_download(
        self, *, timeout: float | None = None
    ) -> AsyncIterator[FakeDownloadHandle]:
        if self._capture_error is not None:
            raise self._capture_error

        if self._never_downloads:
            msg = "no download started"
            raise TimeoutError(msg)

        assert self._download is not None, "this page was not given a download to produce"
        yield FakeDownloadHandle(self._download)


class FakeLocator:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    async def all_text_contents(self) -> list[str]:
        return self._texts

    async def count(self) -> int:
        return len(self._texts)


class FakeNativePage:
    """Stands in for a real Playwright page, one layer below :class:`FakePage`."""

    def __init__(self, texts: dict[str, list[str]] | None = None) -> None:
        self._texts = texts or {}
        self.calls: list[tuple[str, Any, Any]] = []

    async def goto(self, url: str, *, timeout: float | None = None) -> None:
        self.calls.append(("goto", url, timeout))

    async def click(self, selector: str, *, timeout: float | None = None) -> None:
        self.calls.append(("click", selector, timeout))

    async def fill(self, selector: str, value: str, *, timeout: float | None = None) -> None:
        self.calls.append(("fill", (selector, value), timeout))

    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:
        self.calls.append(("wait_for_selector", selector, timeout))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._texts.get(selector, []))

    def expect_download(self, *, timeout: float | None = None) -> str:
        self.calls.append(("expect_download", None, timeout))
        return "download-context"


class FakeContext:
    def __init__(self, options: dict[str, Any], page: FakeNativePage) -> None:
        self.options = options
        self.closed = False
        self._page = page

    async def new_page(self) -> FakeNativePage:
        return self._page

    async def close(self) -> None:
        self.closed = True


_CONTEXT_OPTIONS = frozenset({"accept_downloads", "storage_state", "user_agent"})


class FakeBrowser:
    def __init__(self, page: FakeNativePage) -> None:
        self.contexts: list[FakeContext] = []
        self.closed = False
        self.connected = True
        self._page = page

    async def new_context(self, **options: Any) -> FakeContext:
        # Mirrors the real signature: Playwright raises TypeError for unknown context
        # options, so the fake must too -- otherwise it teaches the wrong contract.
        unknown = set(options) - _CONTEXT_OPTIONS
        if unknown:
            msg = f"new_context() got unexpected keyword arguments: {sorted(unknown)}"
            raise TypeError(msg)

        context = FakeContext(options, self._page)
        self.contexts.append(context)
        return context

    def is_connected(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.closed = True
        self.connected = False


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.launch_options: dict[str, Any] | None = None
        self.launches = 0
        self._browser = browser

    async def launch(self, **options: Any) -> FakeBrowser:
        self.launch_options = options
        self.launches += 1
        return self._browser


class FakeDriver:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightFactory:
    """Stands in for ``playwright.async_api.async_playwright``."""

    def __init__(self, page: FakeNativePage | None = None) -> None:
        self.browser = FakeBrowser(page or FakeNativePage())
        self.driver = FakeDriver(self.browser)

    def __call__(self) -> FakePlaywrightFactory:
        return self

    async def start(self) -> FakeDriver:
        return self.driver
