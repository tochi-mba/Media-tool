"""The HTTP client for keyring."""

from __future__ import annotations

import httpx
import pytest

from media_tool.core.keyring.client import KeyringClient
from media_tool.core.keyring.tokens import JWKS_PATH
from tests.fakes.keyring import SERVICE_TOKEN, FakeKeyring


def make_client(keyring: FakeKeyring) -> KeyringClient:
    return KeyringClient(
        base_url="https://keyring.test",
        service_token=None,
        transport=keyring.transport,
    )


class TestFetchingKeys:
    async def test_it_reads_the_published_key_set(self) -> None:
        keyring = FakeKeyring()
        client = make_client(keyring)

        try:
            document = await client.fetch_jwks()
        finally:
            await client.aclose()

        assert document == keyring.signer.jwks()

    async def test_it_asks_for_the_well_known_path(self) -> None:
        seen: list[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"keys": []})

        client = KeyringClient(
            base_url="https://keyring.test", transport=httpx.MockTransport(record)
        )
        try:
            await client.fetch_jwks()
        finally:
            await client.aclose()

        assert seen == [JWKS_PATH]

    async def test_an_error_response_is_raised_rather_than_returned(self) -> None:
        def unavailable(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "starting up"})

        client = KeyringClient(
            base_url="https://keyring.test", transport=httpx.MockTransport(unavailable)
        )

        # An empty key set silently returned would look like "keyring has no keys",
        # which reads as every token being forged.
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_jwks()
        await client.aclose()

    async def test_an_unreachable_keyring_raises(self) -> None:
        keyring = FakeKeyring()
        keyring.unreachable = True
        client = make_client(keyring)

        with pytest.raises(httpx.ConnectError):
            await client.fetch_jwks()
        await client.aclose()

    async def test_it_holds_the_service_token_it_was_given(self) -> None:
        from pydantic import SecretStr

        client = KeyringClient(
            base_url="https://keyring.test",
            service_token=SecretStr(SERVICE_TOKEN),
            transport=FakeKeyring().transport,
        )
        try:
            assert client._service_token is not None
            assert client._service_token.get_secret_value() == SERVICE_TOKEN
        finally:
            await client.aclose()

    async def test_it_does_not_follow_redirects(self) -> None:
        # This client sends credentials on other endpoints; a redirect is another server
        # asking for them.
        def redirect(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://elsewhere.test/keys"})

        client = KeyringClient(
            base_url="https://keyring.test", transport=httpx.MockTransport(redirect)
        )

        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_jwks()
        await client.aclose()
