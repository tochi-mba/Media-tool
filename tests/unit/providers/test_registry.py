"""Choosing an adapter from configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from media_tool.core.config import ProviderName, Settings
from media_tool.providers.browser.download_provider import BrowserDownloadProvider
from media_tool.providers.registry import UnknownProviderError, build_provider
from media_tool.providers.stub import StubDownloadProvider

if TYPE_CHECKING:
    from pathlib import Path

RECIPE = {
    "name": "example",
    "start_url": "https://example.test/{query}",
    "download_trigger": {"action": "click", "selector": "a.download"},
}


def settings_for(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_the_stub_is_the_default() -> None:
    assert isinstance(build_provider(settings_for()), StubDownloadProvider)


def test_an_unrecognized_provider_is_rejected() -> None:
    settings = settings_for().model_copy(update={"provider": "telepathy"})

    with pytest.raises(UnknownProviderError, match="telepathy"):
        build_provider(settings)


class TestBrowserProvider:
    def test_it_is_built_from_a_recipe(self, tmp_path: Path) -> None:
        recipe_path = tmp_path / "recipe.json"
        recipe_path.write_text(json.dumps(RECIPE), encoding="utf-8")

        provider = build_provider(
            settings_for(
                provider=ProviderName.BROWSER,
                recipe_path=recipe_path,
                artifact_dir=tmp_path / "artifacts",
            )
        )

        assert isinstance(provider, BrowserDownloadProvider)
        assert provider.name == "browser:example"

    def test_it_refuses_to_start_without_a_recipe(self) -> None:
        # Better to fail at startup than to accept work that cannot possibly be done.
        with pytest.raises(UnknownProviderError, match="recipe_path"):
            build_provider(settings_for(provider=ProviderName.BROWSER))

    def test_a_missing_recipe_file_is_reported_at_startup(self, tmp_path: Path) -> None:
        with pytest.raises(UnknownProviderError, match="cannot read recipe"):
            build_provider(
                settings_for(provider=ProviderName.BROWSER, recipe_path=tmp_path / "absent.json")
            )

    def test_an_invalid_recipe_is_reported_at_startup(self, tmp_path: Path) -> None:
        recipe_path = tmp_path / "recipe.json"
        recipe_path.write_text(json.dumps({"name": "x"}), encoding="utf-8")

        with pytest.raises(UnknownProviderError, match="not usable"):
            build_provider(settings_for(provider=ProviderName.BROWSER, recipe_path=recipe_path))
