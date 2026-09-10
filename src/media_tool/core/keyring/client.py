"""Talking to keyring over HTTP.

One client, owned by the container and closed with it, so connections are pooled rather
than opened per request. It holds this service's own token because keyring's internal
endpoints want two credentials: the service's, proving which service is asking, and the
caller's, proving whose credential it may have.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from media_tool.core.keyring.credentials import FormSecrets, ResolvedCredential
from media_tool.core.keyring.tokens import JWKS_PATH
from media_tool.core.logging import get_logger
from media_tool.domain.errors import (
    CredentialNotFoundError,
    KeyringRejectedError,
    KeyringUnavailableError,
)

if TYPE_CHECKING:
    from pydantic import SecretStr

logger = get_logger(__name__)

SERVICE_TOKEN_HEADER = "Authorization"  # noqa: S105 - a header name
USER_TOKEN_HEADER = "X-Keyring-User-Token"  # noqa: S105 - a header name
"""Where the caller's own token travels.

A different header from Authorization because the two say different things: one is which
service is calling, the other is who it is calling for. Keyring insists on both, so that
no service can name an account it was not handed a token for -- including this one.
"""

CREDENTIALS_PATH = "/v1/internal/credentials"
FORM_SECRETS_PATH = "/v1/internal/form-secrets"

NOT_CONFIGURED = "this service has no keyring service token configured"
UNREACHABLE = "keyring could not be reached"
REFUSED = "keyring refused this call"


class KeyringClient:
    """An HTTP client for the keyring endpoints this service uses."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: SecretStr | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._service_token = service_token
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            # Redirects are off deliberately: this client sends credentials, and a
            # redirect is somebody else's server asking for them.
            follow_redirects=False,
        )

    async def fetch_jwks(self) -> dict[str, Any]:
        """Read keyring's published signing keys.

        Unauthenticated, because a verifier has to be able to read them before it can
        verify anything -- including its own credentials.

        Raises:
            httpx.HTTPError: keyring could not be reached or answered with an error.
                The caller decides what an unreadable key set means; see
                :class:`~media_tool.core.keyring.tokens.TokenVerifier`.
        """
        response = await self._http.get(JWKS_PATH)
        response.raise_for_status()
        document: dict[str, Any] = response.json()
        return document

    async def resolve_credential(
        self, *, user_token: str, profile: str, service: str
    ) -> ResolvedCredential:
        """Read what to attach to an outgoing request for the token's owner.

        Raises:
            KeyringRejectedError: keyring would not accept one of the two credentials.
            CredentialNotFoundError: no such profile, or no such connection on it.
            KeyringUnavailableError: keyring could not be reached or could not answer.
        """
        payload = await self._get_internal(
            f"{CREDENTIALS_PATH}/{profile}/{service}", user_token=user_token
        )
        return ResolvedCredential(
            service=str(payload["service"]),
            headers=dict(payload["headers"]),
            query_params=dict(payload["query_params"]),
            expires_at=_moment(payload.get("expires_at")),
        )

    async def resolve_form_secrets(
        self, *, user_token: str, profile: str, service: str
    ) -> FormSecrets:
        """Read the values to type into a site's login form, for the token's owner.

        The one call this service makes that comes back holding credential material.
        What it returns is never stored, never logged, and never put in a response.

        Raises:
            As :meth:`resolve_credential`.
        """
        payload = await self._get_internal(
            f"{FORM_SECRETS_PATH}/{profile}/{service}", user_token=user_token
        )
        return FormSecrets(service=str(payload["service"]), fields=dict(payload["fields"]))

    async def _get_internal(self, path: str, *, user_token: str) -> dict[str, Any]:
        """Call one of keyring's internal endpoints with both required credentials.

        The user's token is forwarded exactly as it arrived. This service does not mint
        tokens and has no way to name an account, which is what makes it unable to ask
        for a credential it was not given one for.
        """
        if self._service_token is None:
            # Not reachable through the app, whose settings refuse to construct without
            # one; reachable by anything that builds a client by hand.
            raise KeyringUnavailableError(NOT_CONFIGURED)

        try:
            response = await self._http.get(
                path,
                headers={
                    SERVICE_TOKEN_HEADER: f"Bearer {self._service_token.get_secret_value()}",
                    USER_TOKEN_HEADER: user_token,
                },
            )
        except httpx.HTTPError as error:
            raise KeyringUnavailableError(UNREACHABLE) from error

        if response.status_code == httpx.codes.UNAUTHORIZED:
            # Which of the two credentials was refused is not on the wire. The caller
            # decides, because only it knows whether the user token was still good.
            raise KeyringRejectedError(REFUSED)
        if response.status_code == httpx.codes.NOT_FOUND:
            msg = f"keyring has no credential for profile {profile_of(path)!r}"
            raise CredentialNotFoundError(msg)
        if response.is_error:
            msg = f"keyring answered {response.status_code}"
            raise KeyringUnavailableError(msg)

        document: dict[str, Any] = response.json()
        return document

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()


def profile_of(path: str) -> str:
    """The profile segment of an internal path, for an error message that says which."""
    return path.rsplit("/", 2)[-2]


def _moment(value: object) -> datetime | None:
    """Parse keyring's expiry, tolerating its absence and its trailing Z."""
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = [
    "CREDENTIALS_PATH",
    "FORM_SECRETS_PATH",
    "SERVICE_TOKEN_HEADER",
    "USER_TOKEN_HEADER",
    "KeyringClient",
]
