"""Verifying the short-lived tokens keyring issues.

Verification is local. Keyring publishes its public keys at a well-known path; we fetch
them once, cache them, and check signatures ourselves. That costs one network call per
cache period instead of one per request, and the price is that a token stays valid until
it expires even if the session behind it was revoked -- which is the trade keyring made
deliberately when it chose a fifteen-minute lifetime.

Every rejection raises the same error with the same message. Distinguishing "expired"
from "wrong audience" would tell an attacker which part of a forged token to fix next.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jwt
from jwt import PyJWKSet

from media_tool.core.logging import get_logger
from media_tool.domain.accounts import AccountId
from media_tool.domain.errors import (
    AuthenticationError,
    InvalidAccountIdError,
    KeyringUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from media_tool.core.clock import Clock

logger = get_logger(__name__)

JWKS_PATH = "/.well-known/jwks.json"
"""Where keyring publishes its public keys. Unauthenticated by necessity."""

ALGORITHM = "RS256"
"""Matches what keyring signs with. Pinned so a token cannot select its own algorithm."""

REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")
"""Absent any one of these the token is not one of keyring's, whatever it is signed by."""

BAD_TOKEN = "the token was not accepted"  # noqa: S105 - a message, not a credential

KEYRING_UNREACHABLE = "keyring's signing keys could not be read"


class TokenVerifier:
    """Turns a bearer token into the account it was minted for."""

    def __init__(
        self,
        *,
        fetch_jwks: Callable[[], Awaitable[dict[str, Any]]],
        issuer: str,
        audience: str,
        clock: Clock,
        cache_ttl_seconds: float,
    ) -> None:
        self._fetch_jwks = fetch_jwks
        self._issuer = issuer
        self._audience = audience
        self._clock = clock
        self._cache_ttl = cache_ttl_seconds
        # The key set and the moment it was fetched are one value, because a key set
        # without its age cannot be judged and an age without keys means nothing.
        self._cached: tuple[PyJWKSet, float] | None = None

    async def account_for(self, token: str) -> AccountId:
        """Return the account a token was issued for.

        Raises:
            AuthenticationError: the token is missing, malformed, expired, signed by an
                unknown key, or was minted for a different issuer or audience.
            KeyringUnavailableError: the keys could not be read and none are cached.
        """
        key = await self._key_for(token)

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                audience=self._audience,
                # Expiry is checked below against the injected clock instead; see
                # _reject_if_expired.
                options={"require": list(REQUIRED_CLAIMS), "verify_exp": False},
            )
        except jwt.PyJWTError as error:
            raise AuthenticationError(BAD_TOKEN) from error

        self._reject_if_expired(claims)

        try:
            return AccountId.parse(claims["sub"])
        except InvalidAccountIdError as error:
            # A subject we would refuse to write down is a token we refuse, and it is
            # refused in the same words as every other bad token.
            raise AuthenticationError(BAD_TOKEN) from error

    def _reject_if_expired(self, claims: dict[str, Any]) -> None:
        """Check ``exp`` against the injected clock rather than PyJWT's.

        PyJWT reads the wall clock and nothing in this codebase does; every component
        takes a :class:`Clock` so that expiry is exercised by moving time rather than by
        waiting for it. The two agree in production, where that clock is the system one.
        """
        try:
            expires_at = float(claims["exp"])
        except (TypeError, ValueError) as error:
            raise AuthenticationError(BAD_TOKEN) from error

        if self._clock.now().timestamp() >= expires_at:
            raise AuthenticationError(BAD_TOKEN)

    async def _key_for(self, token: str) -> Any:
        """Find the signing key a token names, refetching once if it is unknown."""
        key_id = _key_id_of(token)

        keys, just_fetched = await self._current_keys()
        found = _find(keys, key_id)
        if found is None and not just_fetched:
            # An unknown key id is the ordinary shape of key rotation rather than an
            # attack, so it earns one refetch -- unless this very call filled the cache,
            # in which case we already hold keyring's latest answer and asking again
            # would only repeat it.
            found = _find(await self._refresh_keys(), key_id)

        if found is None:
            raise AuthenticationError(BAD_TOKEN)
        return found

    async def _current_keys(self) -> tuple[PyJWKSet, bool]:
        """The key set, and whether obtaining it went to keyring just now."""
        cached = self._cached
        if cached is not None and self._clock.monotonic() - cached[1] < self._cache_ttl:
            return cached[0], False
        return await self._refresh_keys(), True

    async def _refresh_keys(self) -> PyJWKSet:
        """Read the key set afresh, falling back to a stale cache if that fails.

        Serving cached keys through a brief outage -- or through a bad publish -- is
        strictly better than rejecting valid tokens: the keys are public and change
        rarely, while the failure is transient.
        """
        try:
            document = await self._fetch_jwks()
            keys = PyJWKSet.from_dict(document)
        except Exception as error:
            cached = self._cached
            if cached is not None:
                logger.warning("keyring_jwks_unusable_using_cached_keys", error=str(error))
                return cached[0]
            raise KeyringUnavailableError(KEYRING_UNREACHABLE) from error

        self._cached = (keys, self._clock.monotonic())
        return keys


def _key_id_of(token: str) -> str:
    """The ``kid`` a token claims to be signed by.

    Keyring always sets one, so a token without it is not a token of keyring's.
    """
    try:
        header: dict[str, Any] = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        raise AuthenticationError(BAD_TOKEN) from error

    key_id = header.get("kid")
    if not isinstance(key_id, str):
        raise AuthenticationError(BAD_TOKEN)
    return key_id


def _find(keys: PyJWKSet, key_id: str) -> Any:
    """The public key published under ``key_id``, or ``None`` if there is none."""
    for key in keys.keys:
        if key.key_id == key_id:
            return key.key
    return None


__all__ = ["ALGORITHM", "BAD_TOKEN", "JWKS_PATH", "TokenVerifier"]
