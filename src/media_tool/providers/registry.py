"""Selecting a download provider from configuration.

The composition root asks for a provider by name; nothing else in the service knows
which adapter is live. Adding one means adding a branch here and a value to
:class:`~media_tool.core.config.ProviderName` -- see AGENTS.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from media_tool.core.config import ProviderName
from media_tool.providers.stub import StubDownloadProvider

if TYPE_CHECKING:
    from media_tool.core.config import Settings
    from media_tool.providers.base import DownloadProvider


class UnknownProviderError(ValueError):
    """Configuration names a provider that cannot be built."""


def build_provider(settings: Settings) -> DownloadProvider:
    """Construct the provider named by ``settings``.

    Raises:
        UnknownProviderError: if the name is unrecognized, or its requirements are unmet.
    """
    if settings.provider is ProviderName.STUB:
        return StubDownloadProvider()

    msg = f"unknown provider {settings.provider!r}"
    raise UnknownProviderError(msg)
