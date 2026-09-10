"""What keyring hands back, and what this service is allowed to do with it.

Two shapes, because keyring answers two genuinely different questions. A resolved
credential says *what to attach* to an outgoing request -- headers, query parameters --
and never what is stored. Form secrets are the exception keyring makes for sites with no
API, where a browser has to fill in a login form: those really are a username and a
password, which is why the endpoint that returns them sits behind two credentials and
must never become an assistant tool.

Neither type renders its values. They pass through log records, exception messages and
tracebacks by being nearby, and a repr that prints a password once is a password in a
log file forever.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from media_tool.core.logging import get_logger
from media_tool.domain.errors import (
    AuthenticationError,
    KeyringRejectedError,
    KeyringUnavailableError,
    ReauthenticationRequiredError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from datetime import datetime

    from media_tool.core.keyring.client import KeyringClient
    from media_tool.core.keyring.tokens import TokenVerifier

logger = get_logger(__name__)

EXPIRED_MID_JOB = "the token this download was started with has expired; sign in again and resubmit"

SERVICE_TOKEN_REFUSED = "keyring did not accept this service's own credentials"  # noqa: S105


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedCredential:
    """What to attach to one outgoing request. Not what is stored."""

    service: str
    headers: Mapping[str, str]
    query_params: Mapping[str, str]
    expires_at: datetime | None = None

    def __repr__(self) -> str:
        """Name the keys, never the values. An Authorization header is a credential."""
        return (
            f"ResolvedCredential(service={self.service!r}, "
            f"headers=<{len(self.headers)} redacted>, "
            f"query_params=<{len(self.query_params)} redacted>, "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FormSecrets:
    """The values to type into a login form.

    The one thing in this service that is credential material rather than a reference to
    some. It is held in local scope for the length of one attempt and written nowhere.
    """

    service: str
    fields: Mapping[str, str]

    def __repr__(self) -> str:
        """Field names are useful for diagnosis; field values are never printable."""
        names = ",".join(sorted(self.fields))
        return f"FormSecrets(service={self.service!r}, fields=<{names}: redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class Caller:
    """Who a running job is being done for, and with what.

    Held by the runner for the length of one job and passed down to each attempt. It is
    deliberately not on the :class:`~media_tool.domain.jobs.Job`: the job is the record
    that is stored, read back, and rendered into responses, and a token on it would be a
    token in all three.
    """

    token: str
    """The caller's own short-lived token, forwarded to keyring exactly as it arrived."""

    profile: str
    """Which of their credential sets to use."""

    def __repr__(self) -> str:
        return f"Caller(profile={self.profile!r}, token=<redacted>)"


class CredentialSource(Protocol):
    """Where a running download gets somebody's stored login."""

    async def form_secrets(self, *, user_token: str, profile: str, service: str) -> FormSecrets:
        """Read the login values for one person, profile and service."""
        ...


class KeyringCredentials:
    """Resolves credentials for the person a piece of work is being done for.

    Owns the interpretation the transport cannot do. Keyring answers 401 both for a
    service token it does not recognise and for a user token it will not accept; this
    tells them apart by checking the user's token locally first, using the same verifier
    the edge uses. If that token was good a moment ago, a 401 is keyring refusing *us*,
    which is an operator's problem and not something the caller can fix by signing in
    again.
    """

    def __init__(self, *, client: KeyringClient, verifier: TokenVerifier) -> None:
        self._client = client
        self._verifier = verifier

    async def form_secrets(self, *, user_token: str, profile: str, service: str) -> FormSecrets:
        """Read the login values for one person, profile and service.

        Raises:
            ReauthenticationRequiredError: their token has expired or is no longer good.
            CredentialNotFoundError: they have not connected that service on that profile.
            KeyringUnavailableError: keyring could not answer, or refused this service.
        """
        await self._reject_if_stale(user_token)
        async with _translating_a_refusal():
            return await self._client.resolve_form_secrets(
                user_token=user_token, profile=profile, service=service
            )

    async def credential(
        self, *, user_token: str, profile: str, service: str
    ) -> ResolvedCredential:
        """Read what to attach to an outgoing request, for one person and service.

        Raises:
            As :meth:`form_secrets`.
        """
        await self._reject_if_stale(user_token)
        async with _translating_a_refusal():
            return await self._client.resolve_credential(
                user_token=user_token, profile=profile, service=service
            )

    async def _reject_if_stale(self, user_token: str) -> None:
        """Refuse before the network call if the token this job is holding has aged out.

        Short-lived tokens outlive very little, and a download can outlast one. Saying so
        here makes it an actionable outcome rather than a 401 from keyring that could
        mean either party.
        """
        try:
            await self._verifier.account_for(user_token)
        except AuthenticationError as error:
            raise ReauthenticationRequiredError(EXPIRED_MID_JOB) from error


@asynccontextmanager
async def _translating_a_refusal() -> AsyncIterator[None]:
    """Read keyring's 401 as "keyring refused us", the only reading left inside here.

    Whoever enters this has already established that the user's token verifies, so a
    refusal is about this service's own credentials -- an operator's problem, and not
    something the caller can fix by signing in again.
    """
    try:
        yield
    except KeyringRejectedError as error:
        # Warning rather than exception: the traceback adds nothing, and the request
        # that produced it carries an Authorization header we would rather not render.
        logger.warning("keyring_refused_our_service_token")
        raise KeyringUnavailableError(SERVICE_TOKEN_REFUSED) from error


__all__ = ["FormSecrets", "KeyringCredentials", "ResolvedCredential"]
