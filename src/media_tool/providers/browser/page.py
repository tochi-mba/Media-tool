"""The browser port.

Deliberately narrow. It names only the handful of things the download flow actually
does -- navigate, wait, click, fill, read text, capture a download -- rather than
re-exposing Playwright. That keeps the fake used in tests small enough to be obviously
correct, and means a different automation library could be dropped in behind it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path


class DownloadLike(Protocol):
    """A file the browser started downloading."""

    @property
    def suggested_filename(self) -> str:
        """The name the site asked for. Hostile input: sanitized before use."""
        ...

    @property
    def url(self) -> str:
        """Where the bytes actually came from."""
        ...

    async def save_as(self, path: str | Path) -> None:
        """Write the download to ``path``."""
        ...


class DownloadHandle(Protocol):
    """The pending result of an action expected to start a download."""

    @property
    async def value(self) -> DownloadLike:
        """Await the download once the triggering action has run."""
        ...


class PageLike(Protocol):
    """One browser page."""

    async def goto(self, url: str, *, timeout: float | None = None) -> None:
        """Navigate, waiting for the document to load."""
        ...

    async def click(self, selector: str, *, timeout: float | None = None) -> None: ...

    async def fill(self, selector: str, value: str, *, timeout: float | None = None) -> None: ...

    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:
        """Wait for an element to appear.

        Raises:
            Exception: the underlying driver's timeout error if it never does.
        """
        ...

    async def text_contents(self, selector: str) -> list[str]:
        """Return the text of every element matching ``selector``."""
        ...

    async def query_count(self, selector: str) -> int:
        """How many elements match ``selector``."""
        ...

    def expect_download(
        self, *, timeout: float | None = None
    ) -> AbstractAsyncContextManager[DownloadHandle]:
        """Capture the download started by the action performed inside the block.

        This is what replaces a human clicking and choosing "Save": the click happens
        inside the block, and the file arrives on the handle.
        """
        ...


class BrowserRuntime(Protocol):
    """Supplies pages and owns the browser process."""

    def acquire_page(self) -> AbstractAsyncContextManager[PageLike]:
        """Check out a page, returning it when the block exits.

        Concurrency is capped inside: pages cost real memory, so a large batch waits
        rather than opening one page per item.
        """
        ...

    async def healthy(self) -> bool:
        """Whether a browser can currently be started or is already running."""
        ...

    async def aclose(self) -> None:
        """Shut the browser down. Must be safe to call twice."""
        ...
