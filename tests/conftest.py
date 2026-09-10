"""Shared fixtures.

Every test that touches the app builds its own, against a temporary artifact directory,
so nothing leaks between cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from media_tool.api.app import create_app
from media_tool.core.config import LogFormat, Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a scratch directory, with timings tuned for tests."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        artifact_dir=tmp_path / "artifacts",
        log_format=LogFormat.CONSOLE,
        job_sweep_interval_seconds=3600,
        download_concurrency=4,
        max_attempts=1,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.01,
        download_timeout_seconds=5,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired straight to the ASGI app, with lifespan run for real."""
    async with (
        LifespanManager(app) as managed,
        AsyncClient(
            transport=ASGITransport(app=managed.app), base_url="http://media-tool.test"
        ) as http,
    ):
        yield http


def container_of(app: FastAPI) -> Any:
    """Reach the wired container, for tests that need to inspect or substitute an adapter."""
    return app.state.container
