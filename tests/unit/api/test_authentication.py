"""Who a request belongs to, decided once, at the edge.

This is the boundary the whole multi-tenant story rests on. Everything below it is
written as though an account always exists and is always the right one, so the tests
here are about the two ways that could be false: letting somebody in who should not be,
and getting the account wrong for somebody who should.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from media_tool.api.app import create_app
from media_tool.api.dependencies import AccountDep, current_account
from media_tool.api.middleware import PUBLIC_PATHS
from media_tool.core.config import Settings
from media_tool.core.container import Container
from media_tool.domain.errors import AuthenticationError
from tests.fakes.accounts import ACCOUNT_ID, OTHER_ACCOUNT_ID
from tests.fakes.keyring import FakeKeyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

PROTECTED = "/v1/downloads"


@pytest.fixture
async def probe(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client against a route that reports the account it was served as."""

    async def whoami(account: AccountDep) -> dict[str, str]:
        return {"account": str(account)}

    app.add_api_route("/whoami", whoami, methods=["GET"])

    async with (
        LifespanManager(app) as managed,
        AsyncClient(
            transport=ASGITransport(app=managed.app), base_url="http://media-tool.test"
        ) as http,
    ):
        yield http


class TestRefusal:
    async def test_a_request_with_no_token_is_refused(self, anonymous_client: AsyncClient) -> None:
        response = await anonymous_client.get(PROTECTED)

        assert response.status_code == 401

    async def test_the_refusal_is_a_problem_document(self, anonymous_client: AsyncClient) -> None:
        response = await anonymous_client.get(PROTECTED)

        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["status"] == 401

    async def test_the_refusal_carries_a_challenge(self, anonymous_client: AsyncClient) -> None:
        # RFC 9110 requires a 401 to say what would be accepted.
        response = await anonymous_client.get(PROTECTED)

        assert response.headers["www-authenticate"] == "Bearer"

    async def test_the_refusal_carries_a_request_id(self, anonymous_client: AsyncClient) -> None:
        # The context middleware wraps authentication rather than the other way round,
        # so a rejected request is as traceable as a served one.
        response = await anonymous_client.get(PROTECTED)

        assert response.headers["x-request-id"]
        assert response.json()["request_id"] == response.headers["x-request-id"]

    @pytest.mark.parametrize(
        "header",
        [
            "Basic dXNlcjpwYXNz",
            "bearer lowercase-scheme-is-not-ours",
            "sometoken",
            "",
        ],
    )
    async def test_credentials_that_are_not_a_bearer_token_are_refused(
        self, anonymous_client: AsyncClient, header: str
    ) -> None:
        response = await anonymous_client.get(PROTECTED, headers={"Authorization": header})

        assert response.status_code == 401

    async def test_a_token_for_another_service_is_refused(
        self, anonymous_client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        token = keyring.token_for(audience="some-other-service")

        response = await anonymous_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 401

    async def test_an_expired_token_is_refused(
        self, anonymous_client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        token = keyring.token_for(ttl_seconds=-1)

        response = await anonymous_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 401

    async def test_an_unknown_path_is_refused_before_it_is_looked_up(
        self, anonymous_client: AsyncClient
    ) -> None:
        # A 404 here would answer "does this route exist" for anyone who asks, which is
        # a map of the service handed out for free.
        response = await anonymous_client.get("/v1/not-a-real-route")

        assert response.status_code == 401

    async def test_the_refusal_does_not_say_which_check_failed(
        self, anonymous_client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        details = set()
        for header in (
            None,
            f"Bearer {keyring.token_for(audience='elsewhere')}",
            f"Bearer {keyring.token_for(ttl_seconds=-1)}",
            "Bearer not-a-token",
        ):
            headers = {} if header is None else {"Authorization": header}
            response = await anonymous_client.get(PROTECTED, headers=headers)
            details.add(response.json()["detail"])

        # Two: "no bearer token" and "the token was not accepted". Neither says more.
        assert len(details) == 2


class TestPublicPaths:
    async def test_health_needs_no_token(self, anonymous_client: AsyncClient) -> None:
        # A load balancer has no account.
        response = await anonymous_client.get("/healthy")

        assert response.status_code == 200

    async def test_the_schema_needs_no_token(self, anonymous_client: AsyncClient) -> None:
        response = await anonymous_client.get("/openapi.json")

        assert response.status_code == 200

    async def test_a_protected_path_is_still_protected(self, anonymous_client: AsyncClient) -> None:
        # The mirror of the two above: being on the list is what makes a path open, and
        # nothing else does.
        assert (await anonymous_client.get("/v1/downloads/anything")).status_code == 401

    def test_the_allowlist_is_exactly_what_is_meant_to_be_open(self) -> None:
        # Pinned so that opening a path is a deliberate edit to this list, seen in review,
        # rather than a side effect of adding a route.
        expected = {"/healthy", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

        assert expected == PUBLIC_PATHS


class TestAttribution:
    async def test_the_handler_is_told_who_is_asking(
        self, probe: AsyncClient, keyring: FakeKeyring
    ) -> None:
        response = await probe.get(
            "/whoami", headers={"Authorization": keyring.authorization_for(ACCOUNT_ID)}
        )

        assert response.json() == {"account": ACCOUNT_ID}

    async def test_a_different_token_is_a_different_account(
        self, probe: AsyncClient, keyring: FakeKeyring
    ) -> None:
        response = await probe.get(
            "/whoami", headers={"Authorization": keyring.authorization_for(OTHER_ACCOUNT_ID)}
        )

        assert response.json() == {"account": OTHER_ACCOUNT_ID}

    async def test_the_account_does_not_leak_between_requests(
        self, probe: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # The binding is a context variable, so this is the test that it is reset rather
        # than left set for whoever is served next.
        first = await probe.get(
            "/whoami", headers={"Authorization": keyring.authorization_for(ACCOUNT_ID)}
        )
        second = await probe.get(
            "/whoami", headers={"Authorization": keyring.authorization_for(OTHER_ACCOUNT_ID)}
        )

        assert first.json()["account"] == ACCOUNT_ID
        assert second.json()["account"] == OTHER_ACCOUNT_ID

    async def test_the_completion_record_says_whose_request_it_was(
        self, client: AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The context middleware wraps the authentication one, so the binding is gone by
        # the time this record is written; it has to be carried on the request instead.
        await client.get("/healthy")
        capsys.readouterr()

        await client.post("/v1/downloads", json={"items": [{"name": "Dune"}]})

        completed = [
            line for line in capsys.readouterr().out.splitlines() if "request_completed" in line
        ]
        assert completed
        assert all(ACCOUNT_ID in line for line in completed)

    async def test_a_public_path_has_no_account_to_report(
        self, anonymous_client: AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        capsys.readouterr()

        await anonymous_client.get("/healthy")

        assert "account" not in capsys.readouterr().out

    def test_asking_for_an_account_outside_a_request_is_refused(self) -> None:
        # The dependency is the second line of defence: if a protected route ever ends up
        # on the public list, this is what stops it being served to nobody in particular.
        with pytest.raises(AuthenticationError):
            current_account()


class TestKeyringDown:
    async def test_an_unreachable_keyring_is_a_503_not_a_401(
        self, anonymous_client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # The caller's token may be perfectly good. Saying "your token is bad" would send
        # them off to re-authenticate against a keyring that cannot answer either.
        token = keyring.token_for()
        keyring.unreachable = True

        response = await anonymous_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 503

    async def test_health_reports_the_outage_rather_than_hiding_it(
        self, anonymous_client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # An instance that has never read keyring's keys cannot attribute a single
        # request, so it should be taken out of rotation rather than left answering 503s
        # one at a time. The body is the same shape either way.
        keyring.unreachable = True

        response = await anonymous_client.get("/healthy")

        assert response.status_code == 503
        assert response.json()["checks"]["identity"]["detail"]["ready"] is False

    async def test_cached_keys_keep_the_service_healthy_through_an_outage(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # Once the keys are in hand, tokens really can still be verified -- so saying
        # "unhealthy" would be taking a working instance out of service.
        await client.post("/v1/downloads", json={"items": [{"name": "Dune"}]})
        keyring.unreachable = True

        response = await client.get("/healthy")

        assert response.status_code == 200
        assert response.json()["checks"]["identity"]["detail"]["ready"] is True


class TestAuthenticationDisabled:
    """The laptop mode. Everything it does is deliberately visible."""

    @pytest.fixture
    def settings(self, tmp_path: object) -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            require_authentication=False,
            anonymous_account="solo",
            artifact_dir=tmp_path / "artifacts",  # type: ignore[operator]
            job_sweep_interval_seconds=3600,
        )

    @pytest.fixture
    async def solo(self, settings: Settings) -> AsyncIterator[AsyncClient]:
        app = create_app(settings)

        async def whoami(account: AccountDep) -> dict[str, str]:
            return {"account": str(account)}

        app.add_api_route("/whoami", whoami, methods=["GET"])

        async with (
            LifespanManager(app) as managed,
            AsyncClient(
                transport=ASGITransport(app=managed.app), base_url="http://media-tool.test"
            ) as http,
        ):
            yield http

    async def test_a_request_without_a_token_is_served(self, solo: AsyncClient) -> None:
        # 405, not 401: the request reached routing and was turned away by the route's
        # own methods rather than at the door.
        assert (await solo.get(PROTECTED)).status_code == 405

    async def test_everyone_is_the_configured_account(self, solo: AsyncClient) -> None:
        assert (await solo.get("/whoami")).json() == {"account": "solo"}

    async def test_a_token_is_ignored_rather_than_rejected(
        self, solo: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # A caller that has a token should not start failing because the server was
        # configured not to want one.
        response = await solo.get(
            "/whoami", headers={"Authorization": keyring.authorization_for(ACCOUNT_ID)}
        )

        assert response.json() == {"account": "solo"}

    async def test_a_submission_without_a_token_carries_no_caller(self, solo: AsyncClient) -> None:
        # Nothing to forward, so nothing is forwarded. A recipe that needs a login then
        # fails saying exactly that, which is tested where the runner decides it.
        accepted = await solo.post("/v1/downloads", json={"items": [{"name": "Dune"}]})

        assert accepted.status_code == 202

    def test_no_keyring_client_is_built_at_all(self, settings: Settings) -> None:
        # Nothing to connect to, so nothing that could try -- and no credential source
        # either, which is what makes a login-needing recipe say so rather than run.
        container = Container.build(settings)

        assert container.keyring is None
        assert container.credentials is None
        assert container.authenticator.describes == "disabled"
