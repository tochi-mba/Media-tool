"""Request context middleware."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from media_tool.core.context import get_request_id
from tests.fakes.keyring import FakeKeyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


@pytest.fixture
async def instrumented(app: FastAPI, keyring: FakeKeyring) -> AsyncIterator[AsyncClient]:
    seen: list[str | None] = []

    async def observe() -> dict[str, str | None]:
        seen.append(get_request_id())
        return {"request_id": get_request_id()}

    async def explode() -> None:
        msg = "handler blew up before any response was produced"
        raise RuntimeError(msg)

    app.add_api_route("/observe", observe, methods=["GET"])
    app.add_api_route("/explode", explode, methods=["GET"])

    async with (
        LifespanManager(app) as managed,
        AsyncClient(
            # Observe the 500 the way a real client does. Without this, httpx re-raises
            # the handler's exception instead of returning the response Starlette built.
            transport=ASGITransport(app=managed.app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Authorization": keyring.authorization_for()},
        ) as http,
    ):
        yield http


async def test_the_handler_sees_the_bound_request_id(instrumented: AsyncClient) -> None:
    response = await instrumented.get("/observe", headers={"X-Request-ID": "abc"})

    assert response.json()["request_id"] == "abc"


async def test_a_failing_request_is_logged_and_still_reports_the_id(
    instrumented: AsyncClient,
) -> None:
    # The middleware logs and re-raises; the app's handler turns it into a 500.
    response = await instrumented.get("/explode")

    assert response.status_code == 500


async def test_the_binding_does_not_outlive_the_request(instrumented: AsyncClient) -> None:
    await instrumented.get("/observe")

    assert get_request_id() is None
