"""GET /healthy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from media_tool.storage.base import StorageHealth
from tests.conftest import container_of

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient


class TestHealthy:
    async def test_it_reports_ok(self, client: AsyncClient) -> None:
        response = await client.get("/healthy")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_it_reports_the_version_and_environment(self, client: AsyncClient) -> None:
        body = (await client.get("/healthy")).json()

        assert body["version"]
        assert body["environment"] == "local"

    async def test_uptime_is_present_and_sane(self, client: AsyncClient) -> None:
        body = (await client.get("/healthy")).json()

        assert body["uptime_seconds"] >= 0

    async def test_every_dependency_is_reported(self, client: AsyncClient) -> None:
        checks = (await client.get("/healthy")).json()["checks"]

        assert set(checks) == {"job_store", "provider", "storage"}
        assert all(check["status"] == "ok" for check in checks.values())

    async def test_it_names_the_live_provider(self, client: AsyncClient) -> None:
        checks = (await client.get("/healthy")).json()["checks"]

        assert checks["provider"]["detail"]["name"] == "stub"

    async def test_it_reports_free_space(self, client: AsyncClient) -> None:
        storage = (await client.get("/healthy")).json()["checks"]["storage"]

        assert storage["detail"]["free_bytes"] > 0

    async def test_the_request_id_comes_back(self, client: AsyncClient) -> None:
        response = await client.get("/healthy")

        assert response.headers["X-Request-ID"]
        assert float(response.headers["X-Response-Time-Ms"]) >= 0

    async def test_a_supplied_request_id_is_honoured(self, client: AsyncClient) -> None:
        response = await client.get("/healthy", headers={"X-Request-ID": "trace-me"})

        assert response.headers["X-Request-ID"] == "trace-me"

    async def test_an_absurd_request_id_is_truncated(self, client: AsyncClient) -> None:
        response = await client.get("/healthy", headers={"X-Request-ID": "x" * 500})

        assert len(response.headers["X-Request-ID"]) == 64


class TestDegraded:
    async def test_unwritable_storage_degrades_the_service(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        container = container_of(app)
        container.artifacts.health = lambda: StorageHealth(writable=False, free_bytes=0)

        response = await client.get("/healthy")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert response.json()["checks"]["storage"]["status"] == "degraded"

    async def test_an_unready_provider_degrades_the_service(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        container = container_of(app)

        async def not_ready() -> bool:
            return False

        container.provider.healthy = not_ready

        response = await client.get("/healthy")

        assert response.status_code == 503
        assert response.json()["checks"]["provider"]["status"] == "degraded"

    async def test_a_degraded_response_keeps_the_same_shape(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        container_of(app).artifacts.health = lambda: StorageHealth(writable=False, free_bytes=0)

        body = (await client.get("/healthy")).json()

        assert set(body) == {"status", "version", "environment", "uptime_seconds", "checks"}


class TestOpenAPI:
    async def test_the_health_operation_id_is_stable(self, client: AsyncClient) -> None:
        # These become MCP tool names, so a rename is a breaking change.
        schema = (await client.get("/openapi.json")).json()

        assert schema["paths"]["/healthy"]["get"]["operationId"] == "get_health"

    async def test_the_docs_render(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 200


class TestErrorFormat:
    async def test_an_unknown_route_returns_problem_json(self, client: AsyncClient) -> None:
        response = await client.get("/does-not-exist")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_a_problem_carries_the_request_id(self, client: AsyncClient) -> None:
        response = await client.get("/does-not-exist", headers={"X-Request-ID": "trace-me"})

        assert response.json()["request_id"] == "trace-me"

    async def test_a_problem_has_the_rfc_9457_shape(self, client: AsyncClient) -> None:
        body = (await client.get("/does-not-exist")).json()

        assert body["type"].startswith("https://")
        assert body["title"]
        assert body["status"] == 404
        assert body["detail"]


@pytest.mark.parametrize("method", ["post", "put", "delete"])
async def test_wrong_methods_are_rejected_in_the_standard_format(
    client: AsyncClient, method: str
) -> None:
    response = await getattr(client, method)("/healthy")

    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/problem+json")
