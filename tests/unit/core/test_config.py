"""Configuration is the operator-facing contract, so its defaults and bounds are pinned."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from media_tool.core.config import BrowserSettings, ProviderName, Settings, load_settings


def make_settings(**overrides: Any) -> Settings:
    """Build settings without letting a developer's local .env leak into the test."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_defaults_are_safe_for_a_fresh_checkout() -> None:
    settings = make_settings()

    assert settings.app_name == "media-tool"
    assert settings.provider is ProviderName.STUB
    assert settings.max_items_per_request == 50
    assert settings.download_concurrency == 4
    assert settings.max_attempts == 3
    assert settings.browser.headless is True


def test_environment_variables_override_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MEDIA_TOOL_PROVIDER", "browser")
    monkeypatch.setenv("MEDIA_TOOL_MAX_ITEMS_PER_REQUEST", "7")
    monkeypatch.setenv("MEDIA_TOOL_ARTIFACT_DIR", str(tmp_path / "artifacts"))

    settings = load_settings()

    assert settings.provider is ProviderName.BROWSER
    assert settings.max_items_per_request == 7
    assert settings.artifact_dir == tmp_path / "artifacts"


def test_nested_browser_settings_use_a_double_underscore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_TOOL_BROWSER__HEADLESS", "false")
    monkeypatch.setenv("MEDIA_TOOL_BROWSER__MAX_PAGES", "3")
    monkeypatch.setenv("MEDIA_TOOL_BROWSER__EXECUTABLE_PATH", "/opt/pw-browsers/chromium")

    settings = load_settings()

    assert settings.browser.headless is False
    assert settings.browser.max_pages == 3
    assert settings.browser.executable_path == Path("/opt/pw-browsers/chromium")


def test_unknown_settings_are_rejected_rather_than_silently_ignored() -> None:
    with pytest.raises(ValidationError):
        make_settings(definitely_not_a_setting=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_items_per_request", 0),
        ("download_concurrency", 0),
        ("max_attempts", 0),
        ("max_file_bytes", 0),
        ("job_ttl_seconds", 0),
        ("download_timeout_seconds", 0),
    ],
)
def test_bounds_reject_nonsense(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_backoff_ceiling_may_not_sit_below_the_base_delay() -> None:
    with pytest.raises(ValidationError, match="backoff_max_seconds"):
        make_settings(backoff_base_seconds=5.0, backoff_max_seconds=1.0)


def test_browser_page_cap_is_bounded() -> None:
    with pytest.raises(ValidationError):
        BrowserSettings(max_pages=0)


def test_artifact_dir_is_resolved_to_an_absolute_path() -> None:
    settings = make_settings(artifact_dir=Path("var/artifacts"))

    assert settings.artifact_dir.is_absolute()
