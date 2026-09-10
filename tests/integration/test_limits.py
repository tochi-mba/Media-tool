"""Per-account limits, over HTTP.

The property that matters is not that a limit exists but that it is somebody's: one
person hitting theirs must leave everyone else able to work.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import partial
from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from media_tool.api.app import create_app
from media_tool.core.config import LogFormat, Settings
from media_tool.core.container import Container
from media_tool.domain.jobs import Job
from media_tool.domain.media import MediaQuery
from tests.fakes.accounts import ACCOUNT_ID, ALICE, OTHER_ACCOUNT_ID
from tests.fakes.keyring import ISSUER, SERVICE_TOKEN, FakeKeyring

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from media_tool.domain.accounts import AccountId

ENDPOINT = "/v1/downloads"
DUNE = {"name": "Dune", "year": 2021}


@pytest.fixture
def tight(tmp_path: Path) -> Settings:
    """Limits low enough to reach in a test, and otherwise the real settings."""
    from pydantic import SecretStr

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        keyring_base_url="https://keyring.test",
        keyring_issuer=ISSUER,
        keyring_service_token=SecretStr(SERVICE_TOKEN),
        artifact_dir=tmp_path / "artifacts",
        log_format=LogFormat.CONSOLE,
        job_sweep_interval_seconds=3600,
        max_active_jobs_per_account=1,
        request_burst_per_account=4,
        requests_per_second_per_account=0.0001,
    )


@pytest.fixture
def limited_app(tight: Settings, keyring: FakeKeyring) -> FastAPI:
    return create_app(
        tight,
        container_factory=partial(Container.build, keyring_transport=keyring.transport),
    )


@pytest.fixture
async def running(limited_app: FastAPI) -> AsyncIterator[FastAPI]:
    async with LifespanManager(limited_app) as managed:
        yield managed.app  # type: ignore[misc]


def client_for(running: FastAPI, keyring: FakeKeyring, account_id: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=running),
        base_url="http://media-tool.test",
        headers={"Authorization": keyring.authorization_for(account_id)},
    )


async def seed_unfinished_job(app: FastAPI, account: AccountId) -> None:
    """Give an account a job that will never finish.

    Seeded through the store rather than submitted, because the stub provider finishes a
    job in microseconds -- a test that raced it would pass or fail by timing rather than
    by the rule it is meant to check.
    """
    container = app.state.container
    await container.jobs.add(
        Job.create(
            account=account,
            queries=[MediaQuery.create(name="Dune")],
            now=container.clock.now(),
        )
    )


class TestJobQuota:
    async def test_a_second_unfinished_job_is_refused(
        self, running: FastAPI, limited_app: FastAPI, keyring: FakeKeyring
    ) -> None:
        await seed_unfinished_job(limited_app, ALICE)

        async with client_for(running, keyring, ACCOUNT_ID) as hers:
            refused = await hers.post(ENDPOINT, json={"items": [DUNE]})

        assert refused.status_code == 429

    async def test_the_refusal_says_which_limit_and_how_long_to_wait(
        self, running: FastAPI, limited_app: FastAPI, keyring: FakeKeyring
    ) -> None:
        await seed_unfinished_job(limited_app, ALICE)

        async with client_for(running, keyring, ACCOUNT_ID) as hers:
            refused = await hers.post(ENDPOINT, json={"items": [DUNE]})

        assert "unfinished jobs" in refused.json()["detail"]
        assert refused.headers["Retry-After"]
        assert refused.headers["content-type"].startswith("application/problem+json")

    async def test_one_persons_limit_is_not_everyone_elses(
        self, running: FastAPI, limited_app: FastAPI, keyring: FakeKeyring
    ) -> None:
        # The whole point. A relative queuing downloads must not stop the rest of the
        # household using the service.
        await seed_unfinished_job(limited_app, ALICE)

        async with (
            client_for(running, keyring, ACCOUNT_ID) as hers,
            client_for(running, keyring, OTHER_ACCOUNT_ID) as his,
        ):
            assert (await hers.post(ENDPOINT, json={"items": [DUNE]})).status_code == 429
            assert (await his.post(ENDPOINT, json={"items": [DUNE]})).status_code == 202

    async def test_a_finished_job_frees_the_slot(
        self, running: FastAPI, keyring: FakeKeyring
    ) -> None:
        async with client_for(running, keyring, ACCOUNT_ID) as hers:
            accepted = await hers.post(ENDPOINT, json={"items": [DUNE]})
            await hers.get(f"{ENDPOINT}/{accepted.json()['job_id']}", params={"wait_seconds": 10})

            assert (await hers.post(ENDPOINT, json={"items": [DUNE]})).status_code == 202


class TestRateLimit:
    async def test_asking_too_fast_is_refused(self, running: FastAPI, keyring: FakeKeyring) -> None:
        async with client_for(running, keyring, ACCOUNT_ID) as hers:
            statuses = [(await hers.get(f"{ENDPOINT}/nope")).status_code for _ in range(6)]

        # The first few are ordinary 404s; the burst runs out and the rest are refused.
        assert statuses[0] == 404
        assert statuses[-1] == 429

    async def test_health_is_never_rate_limited(
        self, running: FastAPI, keyring: FakeKeyring
    ) -> None:
        # It is checked on a timer by something with no account, and a load balancer that
        # gets a 429 takes the service out of rotation.
        async with client_for(running, keyring, ACCOUNT_ID) as hers:
            for _ in range(10):
                await hers.get(f"{ENDPOINT}/nope")

            assert (await hers.get("/healthy")).status_code == 200

    async def test_one_account_cannot_spend_anothers_allowance(
        self, running: FastAPI, keyring: FakeKeyring
    ) -> None:
        async with (
            client_for(running, keyring, ACCOUNT_ID) as hers,
            client_for(running, keyring, OTHER_ACCOUNT_ID) as his,
        ):
            for _ in range(10):
                await hers.get(f"{ENDPOINT}/nope")

            assert (await his.get(f"{ENDPOINT}/nope")).status_code == 404
