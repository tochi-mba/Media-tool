"""Server entry point: ``python -m media_tool`` or the ``media-tool`` script."""

from __future__ import annotations

import uvicorn

from media_tool.core.config import load_settings


def main() -> None:
    """Serve the API using the configured host and port."""
    settings = load_settings()
    uvicorn.run(
        "media_tool.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
