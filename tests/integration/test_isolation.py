"""Two accounts, one service, over HTTP.

The isolation tests are their own file because they are the ones that must not be
skipped or quietly weakened. Each is named for the property it holds rather than the
code that holds it, so that a refactor which breaks the property breaks a test whose
name says what was lost.

Every refusal here is a 404. A 403 would confirm the resource exists, and a job id or a
file path is exactly what somebody probing would be trying to confirm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from httpx import AsyncClient

ENDPOINT = "/v1/downloads"
DUNE = {"name": "Dune", "year": 2021}


async def finished_job(client: AsyncClient) -> dict[str, Any]:
    """Submit one item and wait for it to produce a file."""
    accepted = await client.post(ENDPOINT, json={"items": [DUNE]})
    assert accepted.status_code == 202, accepted.text

    settled = await client.get(
        f"{ENDPOINT}/{accepted.json()['job_id']}", params={"wait_seconds": 10}
    )
    assert settled.status_code == 200, settled.text
    body: dict[str, Any] = settled.json()
    assert body["status"] == "succeeded", body
    return body


class TestOneAccountCannotReadAnothers:
    async def test_job(self, client: AsyncClient, other_client: AsyncClient) -> None:
        hers = await finished_job(client)

        assert (await other_client.get(f"{ENDPOINT}/{hers['job_id']}")).status_code == 404

    async def test_job_by_long_poll(self, client: AsyncClient, other_client: AsyncClient) -> None:
        # The long poll reads the same job by a different door, so it needs the same lock
        # -- and it must refuse immediately rather than hold the request open.
        hers = await finished_job(client)

        response = await other_client.get(
            f"{ENDPOINT}/{hers['job_id']}", params={"wait_seconds": 10}
        )

        assert response.status_code == 404

    async def test_file(self, client: AsyncClient, other_client: AsyncClient) -> None:
        hers = await finished_job(client)
        link = hers["results"][0]["links"]["file"]

        assert (await other_client.get(link)).status_code == 404

    async def test_cancellation(self, client: AsyncClient, other_client: AsyncClient) -> None:
        # Not reading, but the same rule: a stranger must not be able to stop her work.
        hers = await finished_job(client)

        assert (await other_client.delete(f"{ENDPOINT}/{hers['job_id']}")).status_code == 404


class TestTheRefusalRevealsNothing:
    async def test_it_is_the_same_status_as_a_job_that_never_existed(
        self, client: AsyncClient, other_client: AsyncClient
    ) -> None:
        hers = await finished_job(client)

        real = await other_client.get(f"{ENDPOINT}/{hers['job_id']}")
        invented = await other_client.get(f"{ENDPOINT}/0123456789abcdef0123456789abcdef")

        assert real.status_code == invented.status_code == 404

    async def test_it_never_says_403(self, client: AsyncClient, other_client: AsyncClient) -> None:
        # Spelled out as its own test because "403 Forbidden" is the intuitive answer and
        # the wrong one: it is an oracle for which job ids are real.
        hers = await finished_job(client)

        for response in (
            await other_client.get(f"{ENDPOINT}/{hers['job_id']}"),
            await other_client.delete(f"{ENDPOINT}/{hers['job_id']}"),
            await other_client.get(hers["results"][0]["links"]["file"]),
        ):
            assert response.status_code != 403

    async def test_the_body_does_not_echo_the_job_back(
        self, client: AsyncClient, other_client: AsyncClient
    ) -> None:
        hers = await finished_job(client)

        body = (await other_client.get(f"{ENDPOINT}/{hers['job_id']}")).json()

        assert "Dune" not in str(body)


class TestEachAccountKeepsItsOwn:
    async def test_both_can_work_at_the_same_time(
        self, client: AsyncClient, other_client: AsyncClient
    ) -> None:
        hers = await finished_job(client)
        his = await finished_job(other_client)

        assert hers["job_id"] != his["job_id"]
        assert (await client.get(f"{ENDPOINT}/{hers['job_id']}")).status_code == 200
        assert (await other_client.get(f"{ENDPOINT}/{his['job_id']}")).status_code == 200

    async def test_each_gets_its_own_file(
        self, client: AsyncClient, other_client: AsyncClient
    ) -> None:
        hers = await finished_job(client)
        his = await finished_job(other_client)

        assert (await client.get(hers["results"][0]["links"]["file"])).status_code == 200
        assert (await other_client.get(his["results"][0]["links"]["file"])).status_code == 200
