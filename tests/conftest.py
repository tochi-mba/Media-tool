"""Shared fixtures.

Every test that touches the app builds its own, against a temporary artifact directory,
so nothing leaks between cases.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from media_tool.api.app import create_app
from media_tool.core.config import LogFormat, Settings
from media_tool.core.container import Container
from tests.fakes.keyring import ACCOUNT_ID, ISSUER, OTHER_ACCOUNT_ID, SERVICE_TOKEN, FakeKeyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

KEYRING_URL = "https://keyring.test"


@pytest.fixture
def keyring_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two variables a deployment must set, in the environment.

    For the tests that go through :func:`load_settings` rather than constructing
    settings directly: without these the service refuses to start, which is the point.
    """
    monkeypatch.setenv("MEDIA_TOOL_KEYRING_BASE_URL", KEYRING_URL)
    monkeypatch.setenv("MEDIA_TOOL_KEYRING_SERVICE_TOKEN", SERVICE_TOKEN)


@pytest.fixture
def keyring() -> FakeKeyring:
    """A keyring that signs real tokens and never opens a socket."""
    return FakeKeyring()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a scratch directory, with timings tuned for tests."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        keyring_base_url=KEYRING_URL,
        keyring_issuer=ISSUER,
        keyring_service_token=SecretStr(SERVICE_TOKEN),
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
def app(settings: Settings, keyring: FakeKeyring) -> FastAPI:
    """The real app, wired to the in-process keyring."""
    return create_app(
        settings,
        container_factory=partial(Container.build, keyring_transport=keyring.transport),
    )


@pytest.fixture
async def running_app(app: FastAPI) -> AsyncIterator[FastAPI]:
    """The app with its lifespan run for real, once per test.

    Every client fixture builds on this one rather than starting its own, so a test that
    wants two callers gets two callers against one service -- not two services.
    """
    async with LifespanManager(app) as managed:
        yield managed.app  # type: ignore[misc]


def _client_for(running: FastAPI, authorization: str | None) -> AsyncClient:
    headers = {"Authorization": authorization} if authorization is not None else {}
    return AsyncClient(
        transport=ASGITransport(app=running),
        base_url="http://media-tool.test",
        headers=headers,
    )


@pytest.fixture
async def anonymous_client(running_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that presents no credentials. For testing what happens to one."""
    async with _client_for(running_app, None) as http:
        yield http


@pytest.fixture
async def client(running_app: FastAPI, keyring: FakeKeyring) -> AsyncIterator[AsyncClient]:
    """A client carrying a valid token, which is what every ordinary request has.

    The default for the whole suite on purpose: an endpoint test that quietly ran
    unauthenticated would stop testing the thing the endpoint does in production.
    """
    async with _client_for(running_app, keyring.authorization_for(ACCOUNT_ID)) as http:
        yield http


@pytest.fixture
async def other_client(running_app: FastAPI, keyring: FakeKeyring) -> AsyncIterator[AsyncClient]:
    """A second account, against the same service, for proving the two stay apart."""
    async with _client_for(running_app, keyring.authorization_for(OTHER_ACCOUNT_ID)) as http:
        yield http


def container_of(app: FastAPI) -> Any:
    """Reach the wired container, for tests that need to inspect or substitute an adapter."""
    return app.state.container
