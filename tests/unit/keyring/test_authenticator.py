"""Turning credentials on a request into an account.

Two implementations of one port, and the tests are mostly about the difference between
them being total: whichever one is wired, everything downstream gets an account.
"""

from __future__ import annotations

import pytest

from media_tool.core.keyring.authenticator import (
    Authenticator,
    KeyringAuthenticator,
    SingleAccountAuthenticator,
)
from media_tool.core.keyring.tokens import TokenVerifier
from media_tool.domain.accounts import AccountId
from media_tool.domain.errors import AuthenticationError
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import ACCOUNT_ID, AUDIENCE, ISSUER, FakeKeyringSigner


@pytest.fixture
def signer() -> FakeKeyringSigner:
    return FakeKeyringSigner()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def keyring_auth(signer: FakeKeyringSigner, clock: FakeClock) -> KeyringAuthenticator:
    async def fetch_jwks() -> dict[str, object]:
        return signer.jwks()

    return KeyringAuthenticator(
        TokenVerifier(
            fetch_jwks=fetch_jwks,
            issuer=ISSUER,
            audience=AUDIENCE,
            clock=clock,
            cache_ttl_seconds=300,
        )
    )


class TestKeyringAuthenticator:
    async def test_a_bearer_token_yields_its_account(
        self, keyring_auth: KeyringAuthenticator, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        header = f"Bearer {signer.issue(now=clock.now())}"

        assert await keyring_auth.account_for(header) == AccountId.parse(ACCOUNT_ID)

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "Basic dXNlcjpwYXNz",
            "bearer wrong-case",
            "Bearer",
            "a-bare-token-with-no-scheme",
        ],
    )
    async def test_anything_that_is_not_a_bearer_token_is_refused(
        self, keyring_auth: KeyringAuthenticator, header: str | None
    ) -> None:
        # One accepted credential kind, stated plainly. "Bearer" without a space is in
        # the list because a prefix match that ignored the separator would accept
        # "Bearertoken" too.
        with pytest.raises(AuthenticationError):
            await keyring_auth.account_for(header)

    async def test_the_token_is_taken_verbatim_after_the_scheme(
        self, keyring_auth: KeyringAuthenticator, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # A stray space would corrupt the token rather than fail loudly, so the split has
        # to be exactly the scheme's length.
        token = signer.issue(account_id="acct_precise", now=clock.now())

        account = await keyring_auth.account_for(f"Bearer {token}")

        assert account == AccountId.parse("acct_precise")

    def test_it_describes_itself_for_health(self, keyring_auth: KeyringAuthenticator) -> None:
        assert keyring_auth.describes == "keyring"

    def test_it_satisfies_the_port(self, keyring_auth: KeyringAuthenticator) -> None:
        assert isinstance(keyring_auth, Authenticator)


class TestSingleAccountAuthenticator:
    async def test_every_request_is_the_same_account(self) -> None:
        solo = SingleAccountAuthenticator(AccountId.parse("local"))

        assert await solo.account_for(None) == AccountId.parse("local")

    async def test_a_presented_token_is_ignored_not_rejected(self) -> None:
        solo = SingleAccountAuthenticator(AccountId.parse("local"))

        assert await solo.account_for("Bearer whatever") == AccountId.parse("local")

    def test_it_describes_itself_for_health(self) -> None:
        assert SingleAccountAuthenticator(AccountId.parse("local")).describes == "disabled"

    def test_it_satisfies_the_port(self) -> None:
        # The point of the port: the middleware cannot tell which of the two it has.
        assert isinstance(SingleAccountAuthenticator(AccountId.parse("local")), Authenticator)
