"""Resolving somebody's credentials from keyring.

Two things are being tested here and they are worth separating. One is the wire: both
credentials sent, the caller's token forwarded exactly as it arrived, each status code
read as the thing it means. The other is what this service does with what comes back:
hold it in local scope, and never let it reach a log record, a response, or a job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from media_tool.core.keyring.client import (
    CREDENTIALS_PATH,
    FORM_SECRETS_PATH,
    USER_TOKEN_HEADER,
    KeyringClient,
)
from media_tool.core.keyring.credentials import (
    FormSecrets,
    KeyringCredentials,
    ResolvedCredential,
)
from media_tool.core.keyring.tokens import TokenVerifier
from media_tool.domain.errors import (
    CredentialNotFoundError,
    KeyringRejectedError,
    KeyringUnavailableError,
    ReauthenticationRequiredError,
)
from tests.fakes.accounts import ACCOUNT_ID, OTHER_ACCOUNT_ID
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import AUDIENCE, ISSUER, SERVICE_TOKEN, FakeKeyring

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PROFILE = "default"
SERVICE = "somesite"


@pytest.fixture
def keyring() -> FakeKeyring:
    fake = FakeKeyring()
    fake.connect(account_id=ACCOUNT_ID, profile=PROFILE, service=SERVICE)
    return fake


@pytest.fixture
async def client(keyring: FakeKeyring) -> AsyncIterator[KeyringClient]:
    from pydantic import SecretStr

    made = KeyringClient(
        base_url="https://keyring.test",
        service_token=SecretStr(keyring.service_token),
        transport=keyring.transport,
    )
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
def credentials(client: KeyringClient) -> KeyringCredentials:
    return KeyringCredentials(
        client=client,
        verifier=TokenVerifier(
            fetch_jwks=client.fetch_jwks,
            issuer=ISSUER,
            audience=AUDIENCE,
            clock=FakeClock(start=datetime.now(UTC)),
            cache_ttl_seconds=300,
        ),
    )


class TestTheWire:
    async def test_both_credentials_are_sent(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        seen: dict[str, str] = {}

        def record(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"service": SERVICE, "fields": {}})

        client._http = httpx.AsyncClient(
            base_url="https://keyring.test", transport=httpx.MockTransport(record)
        )
        token = keyring.token_for()

        await client.resolve_form_secrets(user_token=token, profile=PROFILE, service=SERVICE)

        assert seen["authorization"] == f"Bearer {SERVICE_TOKEN}"
        assert seen[USER_TOKEN_HEADER.lower()] == token

    async def test_the_users_token_is_forwarded_verbatim(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        # This service mints nothing. What it forwards is exactly what it was handed,
        # which is what makes it unable to ask for a credential it was not given one for.
        token = keyring.token_for()

        await client.resolve_form_secrets(user_token=token, profile=PROFILE, service=SERVICE)

        assert keyring.seen_user_tokens == [token]

    async def test_the_paths_are_the_ones_keyring_publishes(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        assert CREDENTIALS_PATH == "/v1/internal/credentials"
        assert FORM_SECRETS_PATH == "/v1/internal/form-secrets"

        # And they resolve: the fake serves only what the real service serves, so a path
        # this service got wrong would 404 rather than quietly work.
        resolved = await client.resolve_credential(
            user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
        )

        assert resolved.service == SERVICE


class TestResolving:
    async def test_a_credential_is_what_to_attach(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        keyring.connect(
            account_id=ACCOUNT_ID,
            profile=PROFILE,
            service=SERVICE,
            headers={"Authorization": "Bearer BQD..."},
            query_params={"market": "GB"},
            expires_at="2026-09-10T13:00:00Z",
        )

        resolved = await client.resolve_credential(
            user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
        )

        assert resolved == ResolvedCredential(
            service=SERVICE,
            headers={"Authorization": "Bearer BQD..."},
            query_params={"market": "GB"},
            expires_at=datetime(2026, 9, 10, 13, 0, tzinfo=UTC),
        )

    async def test_an_absent_expiry_is_absent_rather_than_guessed(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        # Only OAuth credentials expire; a stored API key has no such date, and inventing
        # one would send the caller back to keyring for no reason.
        resolved = await client.resolve_credential(
            user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
        )

        assert resolved.expires_at is None

    async def test_form_secrets_are_the_fields_to_type(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        keyring.connect(
            account_id=ACCOUNT_ID,
            profile=PROFILE,
            service=SERVICE,
            fields={"username": "somebody", "password": "hunter2", "totp": "123456"},
        )

        secrets = await client.resolve_form_secrets(
            user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
        )

        assert secrets.fields["username"] == "somebody"
        assert secrets.fields["totp"] == "123456"

    async def test_a_credential_is_read_for_the_tokens_owner_and_nobody_else(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        # The account comes from the token. There is no argument for naming one, which is
        # the whole reason a compromised service cannot ask for everybody's credentials.
        with pytest.raises(CredentialNotFoundError):
            await client.resolve_credential(
                user_token=keyring.token_for(OTHER_ACCOUNT_ID),
                profile=PROFILE,
                service=SERVICE,
            )


class TestWhatKeyringRefuses:
    async def test_an_unconnected_service_is_not_found(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        with pytest.raises(CredentialNotFoundError):
            await client.resolve_credential(
                user_token=keyring.token_for(), profile=PROFILE, service="never-connected"
            )

    async def test_a_refused_call_is_reported_as_a_refusal(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        # The transport says what happened and does not guess which credential was at
        # fault, because the wire does not say.
        keyring.service_token = "a-different-secret"  # noqa: S105

        with pytest.raises(KeyringRejectedError):
            await client.resolve_credential(
                user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
            )

    async def test_a_missing_user_token_is_refused(self, client: KeyringClient) -> None:
        # The fake enforces what keyring enforces: one credential is not enough.
        with pytest.raises(KeyringRejectedError):
            await client.resolve_credential(user_token="", profile=PROFILE, service=SERVICE)

    async def test_an_unreachable_keyring_is_unavailable_not_refused(
        self, client: KeyringClient, keyring: FakeKeyring
    ) -> None:
        keyring.unreachable = True

        with pytest.raises(KeyringUnavailableError):
            await client.resolve_credential(
                user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
            )

    async def test_any_other_error_status_is_unavailable(self, keyring: FakeKeyring) -> None:
        from pydantic import SecretStr

        def sealed(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "the vault is sealed"})

        client = KeyringClient(
            base_url="https://keyring.test",
            service_token=SecretStr(SERVICE_TOKEN),
            transport=httpx.MockTransport(sealed),
        )
        try:
            with pytest.raises(KeyringUnavailableError, match="503"):
                await client.resolve_credential(
                    user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
                )
        finally:
            await client.aclose()

    async def test_a_client_with_no_service_token_says_so(self, keyring: FakeKeyring) -> None:
        # Unreachable through the app, whose settings refuse to construct without one.
        client = KeyringClient(base_url="https://keyring.test", transport=keyring.transport)
        try:
            with pytest.raises(KeyringUnavailableError, match="service token"):
                await client.resolve_credential(
                    user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
                )
        finally:
            await client.aclose()


class TestInterpretingARefusal:
    """Keyring answers 401 for either credential. Only this side can tell which."""

    async def test_an_expired_token_asks_the_person_to_sign_in_again(
        self, credentials: KeyringCredentials, keyring: FakeKeyring
    ) -> None:
        # A download can outlive a fifteen-minute token. The person needs to be told
        # that, not told the download failed.
        stale = keyring.token_for(ttl_seconds=-1)

        with pytest.raises(ReauthenticationRequiredError):
            await credentials.form_secrets(user_token=stale, profile=PROFILE, service=SERVICE)

    async def test_it_does_not_call_keyring_with_a_token_it_knows_is_stale(
        self, credentials: KeyringCredentials, keyring: FakeKeyring
    ) -> None:
        stale = keyring.token_for(ttl_seconds=-1)

        with pytest.raises(ReauthenticationRequiredError):
            await credentials.credential(user_token=stale, profile=PROFILE, service=SERVICE)

        assert keyring.seen_user_tokens == []

    async def test_a_refusal_of_a_good_token_is_our_problem_not_theirs(
        self, credentials: KeyringCredentials, keyring: FakeKeyring
    ) -> None:
        # The token verified a moment ago, so this is keyring refusing us. Telling the
        # person to sign in again would send them round a loop that cannot help.
        keyring.service_token = "rotated-and-not-deployed"  # noqa: S105

        with pytest.raises(KeyringUnavailableError, match="own credentials"):
            await credentials.form_secrets(
                user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
            )

    async def test_a_good_token_resolves(
        self, credentials: KeyringCredentials, keyring: FakeKeyring
    ) -> None:
        secrets = await credentials.form_secrets(
            user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
        )

        assert secrets.fields["username"] == "somebody"

    async def test_a_good_token_resolves_a_credential(
        self, credentials: KeyringCredentials, keyring: FakeKeyring
    ) -> None:
        resolved = await credentials.credential(
            user_token=keyring.token_for(), profile=PROFILE, service=SERVICE
        )

        assert resolved.service == SERVICE

    async def test_a_missing_credential_passes_through_unchanged(
        self, credentials: KeyringCredentials, keyring: FakeKeyring
    ) -> None:
        # Not everything is an authentication problem: they are who they say they are and
        # simply have not connected that service.
        with pytest.raises(CredentialNotFoundError):
            await credentials.form_secrets(
                user_token=keyring.token_for(), profile=PROFILE, service="never-connected"
            )


class TestSecretsAreNotPrintable:
    """A repr that renders a password once is a password in a log file forever."""

    def test_form_secrets_do_not_render_their_values(self) -> None:
        secrets = FormSecrets(service=SERVICE, fields={"username": "eve", "password": "hunter2"})

        for rendering in (repr(secrets), str(secrets), f"{secrets}"):
            assert "hunter2" not in rendering
            assert "eve" not in rendering

    def test_form_secrets_still_say_which_fields_they_have(self) -> None:
        # Enough to diagnose "the recipe wanted a field that is not there" without
        # printing any of them.
        secrets = FormSecrets(service=SERVICE, fields={"username": "eve", "password": "hunter2"})

        assert "username" in repr(secrets)
        assert "password" in repr(secrets)

    def test_a_resolved_credential_does_not_render_its_headers(self) -> None:
        # An Authorization header is a credential, just a shorter-lived one.
        resolved = ResolvedCredential(
            service=SERVICE,
            headers={"Authorization": "Bearer BQD-secret"},
            query_params={"key": "also-secret"},
        )

        for rendering in (repr(resolved), str(resolved)):
            assert "BQD-secret" not in rendering
            assert "also-secret" not in rendering

    def test_a_resolved_credential_still_says_what_it_is_for(self) -> None:
        resolved = ResolvedCredential(service=SERVICE, headers={}, query_params={})

        assert SERVICE in repr(resolved)

    def test_neither_appears_in_an_exception_that_carries_one(self) -> None:
        # The realistic leak: a secret held in a local that a traceback renders.
        secrets = FormSecrets(service=SERVICE, fields={"password": "hunter2"})

        assert "hunter2" not in str(RuntimeError(f"could not log in with {secrets}"))
