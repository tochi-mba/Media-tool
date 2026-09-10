"""Choosing an adapter from configuration."""

from __future__ import annotations

import pytest

from media_tool.core.config import Settings
from media_tool.providers.registry import UnknownProviderError, build_provider
from media_tool.providers.stub import StubDownloadProvider


def test_the_stub_is_the_default() -> None:
    provider = build_provider(Settings(_env_file=None))  # type: ignore[call-arg]

    assert isinstance(provider, StubDownloadProvider)


def test_an_unrecognized_provider_is_rejected() -> None:
    settings = Settings(_env_file=None).model_copy(update={"provider": "telepathy"})  # type: ignore[call-arg]

    with pytest.raises(UnknownProviderError, match="telepathy"):
        build_provider(settings)
