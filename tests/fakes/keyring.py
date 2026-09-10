"""A stand-in for the keyring service.

Signs real RS256 tokens with a real generated key and serves a real JWKS document, so the
verification under test does actual cryptography rather than trusting a stub. It is
deliberately as strict as keyring itself: a fake that accepts what the real service
rejects teaches the wrong contract, which is exactly the class of bug the live browser
tests caught in the download provider.

:class:`FakeKeyringSigner` is the crypto on its own, for testing the verifier.
:class:`FakeKeyring` puts it behind an httpx transport so the whole service can be run
against a keyring that never opens a socket.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from media_tool.core.keyring.tokens import JWKS_PATH
from tests.fakes.accounts import ACCOUNT_ID

ALGORITHM = "RS256"
ISSUER = "https://keyring.test"
AUDIENCE = "media-tool"
SERVICE_TOKEN = "service-token-for-media-tool"  # noqa: S105 - a fixture, not a credential


def _merged(base: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """``base`` with ``overrides`` applied, where a ``None`` override removes the key."""
    if overrides is None:
        return base
    merged = {**base, **overrides}
    return {name: value for name, value in merged.items() if value is not None}


def _b64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeKeyringSigner:
    """Mints tokens the way keyring does, and publishes the key that verifies them."""

    def __init__(self, *, issuer: str = ISSUER) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.issuer = issuer
        self.key_id = self._thumbprint()

    def issue(
        self,
        *,
        account_id: str = ACCOUNT_ID,
        audience: str = AUDIENCE,
        issuer: str | None = None,
        ttl_seconds: float = 900,
        now: datetime | None = None,
        key_id: str | None = None,
        claims: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> str:
        """Mint a token.

        The defaults produce exactly what keyring produces. ``claims`` and ``headers``
        are merged over them -- a value of ``None`` drops that claim entirely -- so a
        test can mint the tokens keyring never would and prove they are refused.
        """
        moment = now or datetime.now(UTC)
        payload: dict[str, Any] = {
            "iss": issuer or self.issuer,
            "sub": account_id,
            "aud": audience,
            "iat": int(moment.timestamp()),
            "exp": int((moment + timedelta(seconds=ttl_seconds)).timestamp()),
        }
        return jwt.encode(
            _merged(payload, claims),
            self._private_pem(),
            algorithm=ALGORITHM,
            headers=_merged({"kid": key_id or self.key_id}, headers),
        )

    def jwks(self) -> dict[str, Any]:
        """The public key in the form a verifier expects."""
        numbers = self._key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": ALGORITHM,
                    "kid": self.key_id,
                    "n": _b64url(numbers.n),
                    "e": _b64url(numbers.e),
                }
            ]
        }

    def _private_pem(self) -> bytes:
        return self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def _thumbprint(self) -> str:
        public_pem = self._key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return hashlib.sha256(public_pem).hexdigest()[:16]


class FakeKeyring:
    """The keyring service, in process, reachable over an httpx transport.

    Only the endpoints media-tool actually calls exist, and each one answers exactly what
    the real service answers -- including refusing what it refuses. An unknown path is a
    404 rather than a helpful default, because a fake that invents endpoints lets a
    caller depend on one that is not there.
    """

    def __init__(self, *, issuer: str = ISSUER, service_token: str = SERVICE_TOKEN) -> None:
        self.signer = FakeKeyringSigner(issuer=issuer)
        self.service_token = service_token
        self.jwks_requests = 0
        self.unreachable = False
        """Set to simulate an outage: every request raises, as a dead host does."""

        self.transport = httpx.MockTransport(self._handle)

    def token_for(self, account_id: str = ACCOUNT_ID, **overrides: Any) -> str:
        """Mint a user token for ``account_id``, as keyring's service-token endpoint would."""
        return self.signer.issue(account_id=account_id, **overrides)

    def authorization_for(self, account_id: str = ACCOUNT_ID, **overrides: Any) -> str:
        """The same token, as a complete ``Authorization`` header value."""
        return f"Bearer {self.token_for(account_id, **overrides)}"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            msg = "keyring is unreachable"
            raise httpx.ConnectError(msg)

        if request.url.path == JWKS_PATH:
            self.jwks_requests += 1
            return httpx.Response(200, json=self.signer.jwks())

        return httpx.Response(404, json={"detail": "no such endpoint"})
