"""Deciding which account a request belongs to.

A port with two implementations rather than a flag with two branches. Everything
downstream of the edge -- the middleware, the stores, the runner -- is written once,
against an account that always exists, and the question of where that account came from
stops at this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from media_tool.core.logging import get_logger
from media_tool.domain.errors import AuthenticationError

if TYPE_CHECKING:
    from media_tool.core.keyring.tokens import TokenVerifier
    from media_tool.domain.accounts import AccountId

logger = get_logger(__name__)

BEARER_PREFIX = "Bearer "

MISSING_TOKEN = "this endpoint needs a bearer token issued by keyring"  # noqa: S105 - a message


@runtime_checkable
class Authenticator(Protocol):
    """Turns the credentials on a request into the account making it."""

    async def account_for(self, authorization: str | None, /) -> AccountId:
        """Return the account behind an ``Authorization`` header value.

        The argument is positional-only so that an implementation is free to name it for
        what it does with it -- one of them ignores it entirely.

        Raises:
            AuthenticationError: the header is missing or its credentials are not good.
            KeyringUnavailableError: identity could not be established because keyring
                could not be reached. Deliberately not an authentication failure.
        """
        ...

    @property
    def describes(self) -> str:
        """A word for how this authenticator works, for ``/healthy`` to report."""
        ...


class KeyringAuthenticator:
    """Attributes a request to whoever keyring signed a token for."""

    def __init__(self, verifier: TokenVerifier) -> None:
        self._verifier = verifier

    async def account_for(self, authorization: str | None, /) -> AccountId:
        if authorization is None or not authorization.startswith(BEARER_PREFIX):
            # Also the case for "Basic ..." and for a bare token with no scheme: this
            # service accepts exactly one kind of credential and says so.
            raise AuthenticationError(MISSING_TOKEN)

        return await self._verifier.account_for(authorization[len(BEARER_PREFIX) :])

    @property
    def describes(self) -> str:
        return "keyring"


class SingleAccountAuthenticator:
    """Attributes every request to one fixed account.

    For a laptop, and nowhere else. It makes the service single-tenant again: every
    caller is the same person, so every caller can read every job and every file. The
    container logs a warning when it wires this, and ``/healthy`` reports it, because an
    instance running this way must be impossible to mistake for one that is not.
    """

    def __init__(self, account: AccountId) -> None:
        self._account = account

    async def account_for(self, _authorization: str | None, /) -> AccountId:
        # The header is ignored rather than rejected: a caller that has a token should
        # not start failing because the server was configured not to want one.
        return self._account

    @property
    def describes(self) -> str:
        return "disabled"


__all__ = [
    "Authenticator",
    "KeyringAuthenticator",
    "SingleAccountAuthenticator",
]
