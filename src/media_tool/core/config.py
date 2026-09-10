"""Application configuration.

Every knob is an environment variable prefixed ``MEDIA_TOOL_``; nested browser settings
use a double underscore (``MEDIA_TOOL_BROWSER__HEADLESS``). Unknown variables under the
prefix are rejected rather than ignored, so a typo in a deployment surfaces at startup
instead of silently leaving a default in place.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BYTES_PER_MIB = 1024 * 1024

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


class ProviderName(StrEnum):
    """Which adapter fulfils a download request."""

    STUB = "stub"
    """Deterministic fake. Writes a small placeholder file; needs no browser."""

    BROWSER = "browser"
    """Drives headless Chromium through a site recipe and captures the real download."""


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class BrowserSettings(BaseSettings):
    """How the headless browser is launched and how patient it is."""

    model_config = SettingsConfigDict(extra="forbid")

    headless: bool = True

    executable_path: Path | None = None
    """Explicit Chromium binary.

    Set this when the installed Playwright and the available browser build disagree
    about revisions -- supplying a path also bypasses Playwright's revision check.
    Leave unset to use whatever ``PLAYWRIGHT_BROWSERS_PATH`` resolves to.
    """

    max_pages: PositiveInt = 2
    """Concurrent browser pages. Each one costs real memory, so this is deliberately small."""

    navigation_timeout_ms: PositiveInt = 15_000
    action_timeout_ms: PositiveInt = 10_000
    download_timeout_ms: PositiveInt = 120_000

    storage_state_path: Path | None = None
    """Playwright storage state (cookies, local storage) for sites that need a session."""

    user_agent: str | None = None
    args: tuple[str, ...] = ()


class Settings(BaseSettings):
    """The complete runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="MEDIA_TOOL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # -- Identity ----------------------------------------------------------------------
    app_name: str = "media-tool"
    environment: str = "local"

    # -- Observability -----------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- Serving -----------------------------------------------------------------------
    host: str = "127.0.0.1"
    port: PositiveInt = 8000

    # -- Request limits ----------------------------------------------------------------
    max_items_per_request: PositiveInt = 50

    # -- Download orchestration --------------------------------------------------------
    provider: ProviderName = ProviderName.STUB
    download_concurrency: PositiveInt = 4
    download_timeout_seconds: PositiveFloat = 120.0
    max_attempts: PositiveInt = 3
    backoff_base_seconds: PositiveFloat = 0.5
    backoff_max_seconds: PositiveFloat = 8.0

    # -- Retention ---------------------------------------------------------------------
    job_ttl_seconds: PositiveInt = 3_600
    job_sweep_interval_seconds: PositiveFloat = 60.0
    artifact_ttl_seconds: PositiveInt = 3_600

    # -- Storage -----------------------------------------------------------------------
    artifact_dir: Path = Path("var/artifacts")
    max_file_bytes: PositiveInt = 2048 * BYTES_PER_MIB

    # -- Browser adapter ---------------------------------------------------------------
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    recipe_path: Path | None = None
    """Site recipe consumed by the browser provider. Required when ``provider`` is ``browser``."""

    @field_validator("artifact_dir")
    @classmethod
    def _resolve_artifact_dir(cls, value: Path) -> Path:
        """Resolve early so a relative path can't mean two places after a chdir."""
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _check_backoff_window(self) -> Self:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            msg = "backoff_max_seconds must be greater than or equal to backoff_base_seconds"
            raise ValueError(msg)
        return self


def load_settings() -> Settings:
    """Build settings from the environment and ``.env``."""
    return Settings()
