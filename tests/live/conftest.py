"""Fixtures for the tests that drive a real browser.

The site under test is served from this machine on a random port, so these tests need
no network, no proxy, and no cooperation from anyone else's server -- which is what
makes driving a real browser here reproducible rather than flaky.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_SITE = Path(__file__).parent / "site"
CHROMIUM_PATH = Path("/opt/pw-browsers/chromium")


class _AttachmentHandler(SimpleHTTPRequestHandler):
    """Serves the fixture site, marking .bin files as downloads.

    Content-Disposition is what turns a navigation into a download, so it is the whole
    point of using a real server rather than file:// URLs here.
    """

    def end_headers(self) -> None:
        if self.path.endswith(".bin"):
            name = Path(self.path).name
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default stderr logging."""


@pytest.fixture(scope="session")
def site_url() -> Iterator[str]:
    """Serve the fixture site on a random loopback port for the session."""
    handler = partial(_AttachmentHandler, directory=str(FIXTURE_SITE))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host!s}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def chromium_executable() -> str | None:
    """The Chromium to drive.

    Prefers the image's prebuilt browser when present. Passing an explicit path also
    bypasses Playwright's revision check, which matters whenever the installed
    Playwright is newer than the available build -- and means `playwright install` is
    never needed here.
    """
    return str(CHROMIUM_PATH) if CHROMIUM_PATH.exists() else None
