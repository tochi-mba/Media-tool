"""Error translation.

Every failure leaves this service in the same shape, and an unexpected one leaks
nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from media_tool.api.app import create_app
from media_tool.api.errors import register_exception_handlers
from media_tool.domain.errors import (
    ArtifactTooLargeError,
    InvalidJobTransitionError,
    InvalidMediaQueryError,
    JobNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from media_tool.core.config import Settings

RAISERS: dict[str, Exception] = {
    "/boom/not-found": JobNotFoundError("no job 'x'"),
    "/boom/conflict": InvalidJobTransitionError("already finished"),
    "/boom/invalid": InvalidMediaQueryError("name must not be empty"),
    "/boom/too-large": ArtifactTooLargeError("file is 5 bytes over"),
    "/boom/unexpected": RuntimeError("/srv/secrets/token.pem is unreadable"),
}


class _Body(BaseModel):
    """A minimal validated body, so validation reshaping is testable on its own."""

    count: int
    label: str


@pytest.fixture
async def faulty_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """An app whose routes do nothing but raise, one per error type."""
    app = create_app(settings)

    for path, error in RAISERS.items():
        app.add_api_route(path, _raiser(error), methods=["GET"])

    async def validated(body: _Body) -> dict[str, int]:
        return {"count": body.count}

    app.add_api_route("/boom/validated", validated, methods=["POST"])

    # Re-register so the added routes are covered by the same handlers.
    register_exception_handlers(app)

    async with (
        LifespanManager(app) as managed,
        AsyncClient(
            # Observe the 500 the way a real client does. Without this, httpx re-raises
            # the handler's exception instead of returning the response Starlette built.
            transport=ASGITransport(app=managed.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http,
    ):
        yield http


def _raiser(error: Exception) -> Callable[[], Awaitable[None]]:
    async def handler() -> None:
        raise error

    return handler


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/boom/not-found", 404),
        ("/boom/conflict", 409),
        ("/boom/invalid", 422),
        ("/boom/too-large", 413),
    ],
)
async def test_domain_errors_map_to_their_status(
    faulty_client: AsyncClient, path: str, expected_status: int
) -> None:
    response = await faulty_client.get(path)

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_domain_error_explains_itself(faulty_client: AsyncClient) -> None:
    body = (await faulty_client.get("/boom/not-found")).json()

    assert body["detail"] == "no job 'x'"


async def test_an_unexpected_error_becomes_a_500(faulty_client: AsyncClient) -> None:
    response = await faulty_client.get("/boom/unexpected")

    assert response.status_code == 500


async def test_an_unexpected_error_leaks_nothing(faulty_client: AsyncClient) -> None:
    # The exception text can carry paths, hostnames, or credentials.
    body = (await faulty_client.get("/boom/unexpected")).json()

    assert "secrets" not in body["detail"]
    assert "token.pem" not in body["detail"]
    assert body["request_id"]


async def test_an_unexpected_error_still_returns_problem_json(
    faulty_client: AsyncClient,
) -> None:
    response = await faulty_client.get("/boom/unexpected")

    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["X-Request-ID"]


async def test_a_failed_request_can_still_be_correlated(faulty_client: AsyncClient) -> None:
    # The detail is withheld and the caller is told to quote the request id instead, so
    # the id had better be there -- in the body and on the response.
    response = await faulty_client.get("/boom/unexpected", headers={"X-Request-ID": "trace-me"})

    assert response.json()["request_id"] == "trace-me"
    assert response.headers["X-Request-ID"] == "trace-me"


async def test_an_unmapped_status_still_gets_a_title() -> None:
    from media_tool.api.errors import problem_response

    response = problem_response(status_code=418, detail="short and stout")

    assert response.status_code == 418


def test_registering_handlers_twice_is_harmless(settings: Settings) -> None:
    app: FastAPI = create_app(settings)

    register_exception_handlers(app)


class TestValidation:
    """FastAPI's validation errors are reshaped into the one format this API uses."""

    async def test_a_validation_failure_is_a_422_problem(self, faulty_client: AsyncClient) -> None:
        response = await faulty_client.post("/boom/validated", json={"count": "not a number"})

        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_it_lists_every_offending_field(self, faulty_client: AsyncClient) -> None:
        body = (await faulty_client.post("/boom/validated", json={})).json()

        locations = {error["location"] for error in body["errors"]}
        assert locations == {"body.count", "body.label"}

    async def test_each_field_error_explains_itself(self, faulty_client: AsyncClient) -> None:
        body = (await faulty_client.post("/boom/validated", json={"count": 1})).json()

        assert body["errors"][0]["location"] == "body.label"
        assert body["errors"][0]["message"]

    async def test_a_malformed_body_is_rejected_in_the_same_format(
        self, faulty_client: AsyncClient
    ) -> None:
        response = await faulty_client.post(
            "/boom/validated",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 422
        assert response.json()["type"].endswith("validation-failed")
