"""Verifying the tokens keyring issues.

This is the boundary that decides who a request belongs to. Every way a token can be
wrong gets a test, because a verifier that is loose in any one of them hands one person's
jobs and files to another.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from media_tool.core.keyring.tokens import JWKS_PATH, TokenVerifier
from media_tool.domain.accounts import AccountId
from media_tool.domain.errors import AuthenticationError, KeyringUnavailableError
from tests.fakes.accounts import ACCOUNT_ID
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import AUDIENCE, ISSUER, FakeKeyringSigner


@pytest.fixture
def signer() -> FakeKeyringSigner:
    return FakeKeyringSigner()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class RecordingJwks:
    """Serves a JWKS document and counts how often it was asked for."""

    def __init__(self, signer: FakeKeyringSigner) -> None:
        self._signer = signer
        self.fetches = 0
        self.fail_with: Exception | None = None
        self.serve: dict[str, object] | None = None

    async def __call__(self) -> dict[str, object]:
        self.fetches += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.serve if self.serve is not None else self._signer.jwks()


def make_verifier(
    signer: FakeKeyringSigner, clock: FakeClock, **overrides: object
) -> tuple[TokenVerifier, RecordingJwks]:
    jwks = RecordingJwks(signer)
    verifier = TokenVerifier(
        fetch_jwks=jwks,
        issuer=overrides.pop("issuer", ISSUER),  # type: ignore[arg-type]
        audience=overrides.pop("audience", AUDIENCE),  # type: ignore[arg-type]
        clock=clock,
        cache_ttl_seconds=overrides.pop("cache_ttl_seconds", 300),  # type: ignore[arg-type]
    )
    return verifier, jwks


class TestValidTokens:
    async def test_a_good_token_yields_its_subject(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, _ = make_verifier(signer, clock)

        assert await verifier.account_for(signer.issue(now=clock.now())) == AccountId.parse(
            ACCOUNT_ID
        )

    async def test_the_subject_is_whatever_keyring_signed(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, _ = make_verifier(signer, clock)
        token = signer.issue(account_id="acct_someone_else", now=clock.now())

        assert await verifier.account_for(token) == AccountId.parse("acct_someone_else")


class TestRejection:
    """Each of these is a way one account could otherwise be handed another's data."""

    async def test_an_expired_token_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, _ = make_verifier(signer, clock)
        token = signer.issue(ttl_seconds=900, now=clock.now())

        clock.advance(timedelta(seconds=901))

        with pytest.raises(AuthenticationError):
            await verifier.account_for(token)

    async def test_a_token_for_another_service_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # Audience binding is what stops a token minted for one service being replayed
        # at another -- services hold these for the length of a job.
        verifier, _ = make_verifier(signer, clock)
        token = signer.issue(audience="some-other-service", now=clock.now())

        with pytest.raises(AuthenticationError):
            await verifier.account_for(token)

    async def test_a_token_from_another_issuer_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, _ = make_verifier(signer, clock)
        token = signer.issue(issuer="https://not-your-keyring.test", now=clock.now())

        with pytest.raises(AuthenticationError):
            await verifier.account_for(token)

    async def test_a_tampered_token_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, _ = make_verifier(signer, clock)
        header, payload, signature = signer.issue(now=clock.now()).split(".")
        forged = f"{header}.{payload}x.{signature}"

        with pytest.raises(AuthenticationError):
            await verifier.account_for(forged)

    async def test_a_token_signed_by_a_different_key_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, _ = make_verifier(signer, clock)
        impostor = FakeKeyringSigner()
        # Same kid, different key: claiming to be the right key is not being it.
        token = impostor.issue(now=clock.now(), key_id=signer.key_id)

        with pytest.raises(AuthenticationError):
            await verifier.account_for(token)

    @pytest.mark.parametrize("garbage", ["", "not-a-token", "a.b", "a.b.c.d"])
    async def test_malformed_tokens_are_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock, garbage: str
    ) -> None:
        verifier, _ = make_verifier(signer, clock)

        with pytest.raises(AuthenticationError):
            await verifier.account_for(garbage)

    async def test_a_token_without_a_key_id_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # Keyring always names the key it signed with, so a token that does not is not
        # keyring's -- and guessing at a key on its behalf is how "alg: none" bugs start.
        verifier, _ = make_verifier(signer, clock)
        token = signer.issue(now=clock.now(), headers={"kid": None})

        with pytest.raises(AuthenticationError):
            await verifier.account_for(token)

    @pytest.mark.parametrize("expiry", ["soon", None, {"at": 1}])
    async def test_a_token_whose_expiry_is_not_a_number_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock, expiry: object
    ) -> None:
        # `exp` is read by us rather than by PyJWT, so nonsense in it must be refused
        # here; unreadable is not the same as unlimited.
        verifier, _ = make_verifier(signer, clock)
        token = signer.issue(now=clock.now(), claims={"exp": expiry})

        with pytest.raises(AuthenticationError):
            await verifier.account_for(token)

    @pytest.mark.parametrize("subject", ["", 12345, "../acct_bob", "acct/bob"])
    async def test_a_token_without_a_usable_subject_is_refused(
        self, signer: FakeKeyringSigner, clock: FakeClock, subject: object
    ) -> None:
        # The subject becomes the account every job and file is filed under. An empty
        # one namespaces somebody's work under nothing at all, and a separator in it
        # would namespace it under somebody else's.
        verifier, _ = make_verifier(signer, clock)
        token = signer.issue(now=clock.now(), claims={"sub": subject})

        with pytest.raises(AuthenticationError):
            await verifier.account_for(token)

    async def test_every_rejection_says_the_same_thing(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # Which check failed is nobody's business; a distinct message per failure is a
        # probe for what a token needs to look like.
        verifier, _ = make_verifier(signer, clock)
        messages = set()

        for token in (
            signer.issue(audience="elsewhere", now=clock.now()),
            signer.issue(issuer="https://elsewhere.test", now=clock.now()),
            "not-a-token",
        ):
            with pytest.raises(AuthenticationError) as caught:
                await verifier.account_for(token)
            messages.add(str(caught.value))

        assert len(messages) == 1


class TestKeyCaching:
    async def test_keys_are_fetched_once_and_reused(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, jwks = make_verifier(signer, clock)

        for _ in range(3):
            await verifier.account_for(signer.issue(now=clock.now()))

        assert jwks.fetches == 1

    async def test_keys_are_refetched_once_the_cache_expires(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, jwks = make_verifier(signer, clock, cache_ttl_seconds=300)
        await verifier.account_for(signer.issue(now=clock.now()))

        clock.advance(timedelta(seconds=301))
        await verifier.account_for(signer.issue(now=clock.now()))

        assert jwks.fetches == 2

    async def test_an_unknown_key_id_triggers_a_refetch(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # This is how key rotation is survived without a restart: a token signed by a
        # key we have not seen makes us look again rather than fail.
        verifier, jwks = make_verifier(signer, clock)
        await verifier.account_for(signer.issue(now=clock.now()))

        rotated = FakeKeyringSigner()
        jwks._signer = rotated  # keyring rotated its key
        assert await verifier.account_for(rotated.issue(now=clock.now())) == AccountId.parse(
            ACCOUNT_ID
        )
        assert jwks.fetches == 2

    async def test_a_refetch_that_still_lacks_the_key_gives_up(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        verifier, jwks = make_verifier(signer, clock)
        impostor = FakeKeyringSigner()

        with pytest.raises(AuthenticationError):
            await verifier.account_for(impostor.issue(now=clock.now()))

        assert jwks.fetches == 1


class TestKeyringUnreachable:
    async def test_an_unreachable_keyring_is_not_an_authentication_failure(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # A caller with a perfectly good token must not be told their token is bad
        # because our dependency is down. That distinction is what makes the outage
        # diagnosable.
        verifier, jwks = make_verifier(signer, clock)
        jwks.fail_with = ConnectionError("keyring is down")

        with pytest.raises(KeyringUnavailableError):
            await verifier.account_for(signer.issue(now=clock.now()))

    async def test_cached_keys_survive_an_outage(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # The cache has to be due for a refresh for this to prove anything: a fresh
        # cache would never reach keyring, so the outage would go untested.
        verifier, jwks = make_verifier(signer, clock, cache_ttl_seconds=300)
        await verifier.account_for(signer.issue(now=clock.now()))

        clock.advance(timedelta(seconds=301))
        jwks.fail_with = ConnectionError("keyring is down")

        # Signing keys are public and change rarely; refusing good tokens through a
        # short outage would be strictly worse than serving a stale copy of them.
        assert await verifier.account_for(signer.issue(now=clock.now())) == AccountId.parse(
            ACCOUNT_ID
        )
        assert jwks.fetches == 2

    async def test_an_unusable_key_document_is_not_an_authentication_failure(
        self, signer: FakeKeyringSigner, clock: FakeClock
    ) -> None:
        # A keyring that answers with nothing usable is as unavailable as one that does
        # not answer, and saying so is what keeps a bad publish from reading as a
        # service-wide rash of forged tokens.
        verifier, jwks = make_verifier(signer, clock)
        jwks.serve = {"keys": []}

        with pytest.raises(KeyringUnavailableError):
            await verifier.account_for(signer.issue(now=clock.now()))


def test_the_jwks_path_matches_the_well_known_location() -> None:
    assert JWKS_PATH == "/.well-known/jwks.json"
