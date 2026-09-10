"""Selecting a download provider from configuration.

The composition root asks for a provider by name; nothing else in the service knows
which adapter is live. Adding one means adding a branch here and a value to
:class:`~media_tool.core.config.ProviderName` -- see AGENTS.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from media_tool.core.config import ProviderName
from media_tool.providers.browser.download_provider import BrowserDownloadProvider
from media_tool.providers.browser.playwright_runtime import PlaywrightBrowserRuntime
from media_tool.providers.browser.recipes import RecipeError, SiteRecipe
from media_tool.providers.stub import StubDownloadProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from media_tool.core.config import Settings
    from media_tool.providers.base import DownloadProvider

BROWSER_DOWNLOADS_SUBDIR = ".browser-downloads"


class UnknownProviderError(ValueError):
    """Configuration names a provider that cannot be built."""


def build_provider(settings: Settings) -> DownloadProvider:
    """Construct the provider named by ``settings``.

    Raises:
        UnknownProviderError: if the name is unrecognized, or its requirements are unmet.
    """
    builder = _BUILDERS.get(settings.provider)
    if builder is None:
        # Reachable when configuration bypasses enum validation, which is exactly when
        # a clear error matters most.
        msg = f"unknown provider {settings.provider!r}"
        raise UnknownProviderError(msg)

    return builder(settings)


def _build_stub_provider(_settings: Settings) -> DownloadProvider:
    return StubDownloadProvider()


def _build_browser_provider(settings: Settings) -> DownloadProvider:
    """Assemble the browser provider, failing at startup rather than mid-job.

    A missing or broken recipe is a deployment mistake. Surfacing it here means the
    service refuses to start, instead of accepting work it cannot possibly do.
    """
    if settings.recipe_path is None:
        msg = "the browser provider needs recipe_path (MEDIA_TOOL_RECIPE_PATH) to be set"
        raise UnknownProviderError(msg)

    try:
        recipe = SiteRecipe.load(settings.recipe_path)
    except RecipeError as error:
        raise UnknownProviderError(str(error)) from error

    runtime = PlaywrightBrowserRuntime(
        settings.browser,
        downloads_dir=settings.artifact_dir / BROWSER_DOWNLOADS_SUBDIR,
    )
    return BrowserDownloadProvider(runtime=runtime, recipe=recipe)


# Adding a provider means adding a value to ProviderName and an entry here. Nothing
# else in the service needs to change -- see the recipe in AGENTS.md.
_BUILDERS: dict[ProviderName, Callable[[Settings], DownloadProvider]] = {
    ProviderName.STUB: _build_stub_provider,
    ProviderName.BROWSER: _build_browser_provider,
}
