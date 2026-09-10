"""The download API, end to end over HTTP.

These run against the real app with the stub provider, so the whole pipeline -- job,
runner, storage, retention -- is exercised for real, just without a browser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import container_of
from tests.fakes.accounts import ALICE

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient

ENDPOINT = "/v1/downloads"

SEVERANCE = {"name": "Severance", "season": 1, "episode": 3}
DUNE = {"name": "Dune", "year": 2021}


async def submit(client: AsyncClient, *items: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(ENDPOINT, json={"items": list(items)})
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def submit_and_finish(client: AsyncClient, *items: dict[str, Any]) -> dict[str, Any]:
    """Submit a batch and wait for it to settle, using the long-poll."""
    accepted = await submit(client, *items)
    response = await client.get(f"{ENDPOINT}/{accepted['job_id']}", params={"wait_seconds": 10})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class TestSubmitting:
    async def test_a_batch_is_accepted(self, client: AsyncClient) -> None:
        response = await client.post(ENDPOINT, json={"items": [SEVERANCE, DUNE]})

        assert response.status_code == 202
        assert response.json()["item_count"] == 2
        assert response.json()["status"] == "queued"

    async def test_the_location_header_points_at_the_job(self, client: AsyncClient) -> None:
        response = await client.post(ENDPOINT, json={"items": [DUNE]})

        job_id = response.json()["job_id"]
        assert response.headers["Location"] == f"{ENDPOINT}/{job_id}"
        assert response.headers["Retry-After"] == "1"

    async def test_the_self_link_is_fetchable(self, client: AsyncClient) -> None:
        accepted = await submit(client, DUNE)

        assert (await client.get(accepted["links"]["self"])).status_code == 200

    async def test_each_batch_gets_its_own_id(self, client: AsyncClient) -> None:
        first = await submit(client, DUNE)
        second = await submit(client, DUNE)

        assert first["job_id"] != second["job_id"]


class TestValidation:
    async def test_an_empty_batch_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(ENDPOINT, json={"items": []})

        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_a_missing_name_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(ENDPOINT, json={"items": [{"season": 1}]})

        assert response.status_code == 422
        assert any("name" in error["location"] for error in response.json()["errors"])

    async def test_a_blank_name_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(ENDPOINT, json={"items": [{"name": "   "}]})

        assert response.status_code == 422

    @pytest.mark.parametrize(
        "item",
        [
            {"name": "X", "season": -1},
            {"name": "X", "episode": -1},
            {"name": "X", "year": 1200},
            {"name": "X", "year": 3500},
            {"name": "X", "season": "one"},
            {"name": ""},
        ],
    )
    async def test_nonsense_values_are_rejected(
        self, client: AsyncClient, item: dict[str, Any]
    ) -> None:
        response = await client.post(ENDPOINT, json={"items": [item]})

        assert response.status_code == 422

    async def test_unknown_fields_are_rejected(self, client: AsyncClient) -> None:
        # Catches a caller -- or a model -- inventing a parameter that would be ignored.
        response = await client.post(
            ENDPOINT, json={"items": [{"name": "Dune", "quality": "1080p"}]}
        )

        assert response.status_code == 422

    async def test_an_oversized_batch_is_rejected(self, client: AsyncClient) -> None:
        items = [{"name": f"Film {n}"} for n in range(51)]

        response = await client.post(ENDPOINT, json={"items": items})

        assert response.status_code == 422
        assert "at most 50" in response.json()["detail"]

    async def test_a_batch_at_the_limit_is_accepted(self, client: AsyncClient) -> None:
        items = [{"name": f"Film {n}"} for n in range(50)]

        response = await client.post(ENDPOINT, json={"items": items})

        assert response.status_code == 202


class TestInterpretation:
    async def test_a_series_is_recognized(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, SEVERANCE)

        query = job["results"][0]["query"]
        assert query["kind"] == "series"
        assert query["key"] == "series:severance:s1:e3"

    async def test_a_film_is_recognized(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, DUNE)

        assert job["results"][0]["query"]["kind"] == "movie"

    async def test_a_bare_name_is_unknown(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, {"name": "Dune"})

        assert job["results"][0]["query"]["kind"] == "unknown"

    async def test_names_are_normalized(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, {"name": "  The   Wire  "})

        assert job["results"][0]["query"]["name"] == "The Wire"

    async def test_an_episode_without_a_season_is_accepted(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, {"name": "One Piece", "episode": 1071})

        assert job["results"][0]["query"]["kind"] == "series"
        assert job["results"][0]["status"] == "succeeded"


class TestLifecycle:
    async def test_a_job_runs_to_completion(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, SEVERANCE, DUNE)

        assert job["status"] == "succeeded"
        assert job["counts"] == {"total": 2, "succeeded": 2}

    async def test_timestamps_are_recorded(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, DUNE)

        assert job["created_at"]
        assert job["started_at"]
        assert job["completed_at"]

    async def test_results_keep_the_submitted_order(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, {"name": "A"}, {"name": "B"}, {"name": "C"})

        assert [result["query"]["name"] for result in job["results"]] == ["A", "B", "C"]
        assert [result["index"] for result in job["results"]] == [0, 1, 2]

    async def test_an_unknown_job_is_not_found(self, client: AsyncClient) -> None:
        response = await client.get(f"{ENDPOINT}/does-not-exist")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")


class TestLongPoll:
    async def test_waiting_returns_a_finished_job(self, client: AsyncClient) -> None:
        accepted = await submit(client, DUNE)

        response = await client.get(f"{ENDPOINT}/{accepted['job_id']}", params={"wait_seconds": 10})

        assert response.json()["status"] == "succeeded"

    async def test_not_waiting_answers_immediately(self, client: AsyncClient) -> None:
        accepted = await submit(client, DUNE)

        response = await client.get(f"{ENDPOINT}/{accepted['job_id']}")

        assert response.status_code == 200
        assert response.json()["status"] in {"queued", "running", "succeeded"}

    async def test_an_excessive_wait_is_rejected(self, client: AsyncClient) -> None:
        accepted = await submit(client, DUNE)

        response = await client.get(
            f"{ENDPOINT}/{accepted['job_id']}", params={"wait_seconds": 3600}
        )

        assert response.status_code == 422

    async def test_a_negative_wait_is_rejected(self, client: AsyncClient) -> None:
        accepted = await submit(client, DUNE)

        response = await client.get(f"{ENDPOINT}/{accepted['job_id']}", params={"wait_seconds": -1})

        assert response.status_code == 422


class TestDeduplication:
    async def test_a_duplicate_is_reported_at_every_position(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, SEVERANCE, DUNE, SEVERANCE)

        assert job["counts"]["total"] == 3
        assert job["results"][0]["artifact"]["sha256"] == job["results"][2]["artifact"]["sha256"]

    async def test_every_duplicate_gets_a_working_file_link(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, SEVERANCE, SEVERANCE)

        for result in job["results"]:
            response = await client.get(result["links"]["file"])
            assert response.status_code == 200


class TestFiles:
    async def test_a_captured_file_can_be_downloaded(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, DUNE)
        result = job["results"][0]

        response = await client.get(result["links"]["file"])

        assert response.status_code == 200
        assert len(response.content) == result["artifact"]["size_bytes"]

    async def test_the_file_is_served_as_an_attachment(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, DUNE)

        response = await client.get(job["results"][0]["links"]["file"])

        assert "attachment" in response.headers["content-disposition"]
        assert "dune-2021.stub.bin" in response.headers["content-disposition"]

    async def test_the_bytes_match_the_reported_digest(self, client: AsyncClient) -> None:
        import hashlib

        job = await submit_and_finish(client, DUNE)
        result = job["results"][0]

        response = await client.get(result["links"]["file"])

        assert hashlib.sha256(response.content).hexdigest() == result["artifact"]["sha256"]

    async def test_an_unknown_item_index_is_not_found(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, DUNE)

        response = await client.get(f"{ENDPOINT}/{job['job_id']}/items/99/file")

        assert response.status_code == 404

    async def test_a_negative_index_is_rejected(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, DUNE)

        response = await client.get(f"{ENDPOINT}/{job['job_id']}/items/-1/file")

        assert response.status_code == 422

    async def test_a_file_for_an_unknown_job_is_not_found(self, client: AsyncClient) -> None:
        response = await client.get(f"{ENDPOINT}/nope/items/0/file")

        assert response.status_code == 404

    async def test_asking_for_the_file_of_a_failed_item_is_not_found(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        # A failed item exposes no link, but a caller can still construct the URL.
        from media_tool.providers.stub import StubDownloadProvider

        container_of(app).runner._provider = StubDownloadProvider(missing={"movie:dune:2021"})
        job = await submit_and_finish(client, DUNE)

        response = await client.get(f"{ENDPOINT}/{job['job_id']}/items/0/file")

        assert response.status_code == 404
        assert "produced no file" in response.json()["detail"]

    async def test_a_purged_file_is_not_found(self, client: AsyncClient, app: FastAPI) -> None:
        job = await submit_and_finish(client, DUNE)
        container_of(app).artifacts.purge_job(account=ALICE, job_id=job["job_id"])

        response = await client.get(job["results"][0]["links"]["file"])

        assert response.status_code == 404


class TestFailures:
    async def test_a_miss_is_reported_against_the_item(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        from media_tool.providers.stub import StubDownloadProvider

        container = container_of(app)
        container.runner._provider = StubDownloadProvider(missing={"movie:dune:2021"})

        job = await submit_and_finish(client, DUNE)

        assert job["status"] == "failed"
        assert job["results"][0]["error"]["code"] == "not_found"
        assert job["results"][0]["artifact"] is None
        assert job["results"][0]["links"]["file"] is None

    async def test_a_partial_batch_reports_both_outcomes(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        from media_tool.providers.stub import StubDownloadProvider

        container = container_of(app)
        container.runner._provider = StubDownloadProvider(missing={"movie:dune:2021"})

        job = await submit_and_finish(client, DUNE, SEVERANCE)

        assert job["status"] == "partial"
        assert job["counts"] == {"total": 2, "succeeded": 1, "failed": 1}


class TestCancellation:
    async def test_a_job_can_be_cancelled(self, client: AsyncClient) -> None:
        accepted = await submit(client, DUNE)

        response = await client.delete(f"{ENDPOINT}/{accepted['job_id']}")

        assert response.status_code == 202
        assert response.json()["status"] in {"cancelled", "succeeded"}

    async def test_cancelling_a_finished_job_leaves_it_alone(self, client: AsyncClient) -> None:
        job = await submit_and_finish(client, DUNE)

        response = await client.delete(f"{ENDPOINT}/{job['job_id']}")

        assert response.json()["status"] == "succeeded"

    async def test_cancelling_an_unknown_job_is_not_found(self, client: AsyncClient) -> None:
        assert (await client.delete(f"{ENDPOINT}/nope")).status_code == 404


class TestRetention:
    async def test_an_expired_job_is_gone(self, client: AsyncClient, app: FastAPI) -> None:
        job = await submit_and_finish(client, DUNE)
        container = container_of(app)
        await container.jobs.purge_expired(ttl_seconds=0)

        assert (await client.get(f"{ENDPOINT}/{job['job_id']}")).status_code == 404


class TestContract:
    """Operation ids and descriptions become MCP tool names and tool descriptions."""

    async def test_every_operation_id_is_declared_and_stable(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        operation_ids = {
            operation["operationId"]
            for path in schema["paths"].values()
            for operation in path.values()
        }
        assert operation_ids == {
            "get_health",
            "create_download_job",
            "get_download_job",
            "cancel_download_job",
            "fetch_download_file",
        }

    async def test_every_operation_describes_itself(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                assert operation.get("summary"), f"{method} {path} has no summary"
                assert len(operation.get("description", "")) > 40, f"{method} {path}"

    async def test_failures_are_documented_where_they_can_happen(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        job_get = schema["paths"]["/v1/downloads/{job_id}"]["get"]
        assert "404" in job_get["responses"]

    async def test_the_request_body_carries_an_example(self, client: AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()

        request_schema = schema["components"]["schemas"]["CreateDownloadJobRequest"]
        assert request_schema["examples"]
