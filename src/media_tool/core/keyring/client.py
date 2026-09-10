"""Talking to keyring over HTTP.

One client, owned by the container and closed with it, so connections are pooled rather
than opened per request. It holds this service's own token because keyring's internal
endpoints want two credentials: the service's, proving which service is asking, and the
caller's, proving whose credential it may have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from media_tool.core.keyring.tokens import JWKS_PATH
from media_tool.core.logging import get_logger

if TYPE_CHECKING:
    from pydantic import SecretStr

logger = get_logger(__name__)

SERVICE_TOKEN_HEADER = "Authorization"  # noqa: S105 - a header name
USER_TOKEN_HEADER = "X-Keyring-User-Token"  # noqa: S105 - a header name


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

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()


__all__ = ["SERVICE_TOKEN_HEADER", "USER_TOKEN_HEADER", "KeyringClient"]
