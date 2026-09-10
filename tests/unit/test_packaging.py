"""Checks that the shipped files stay consistent with the code.

Documentation drifts silently. These fail loudly instead.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import media_tool
from media_tool.core.config import BrowserSettings, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_VAR = re.compile(r"^#? ?(MEDIA_TOOL_[A-Z0-9_]+)=", re.MULTILINE)


def documented_variables() -> list[str]:
    return ENV_VAR.findall((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))


def real_variables() -> set[str]:
    top = {f"MEDIA_TOOL_{name.upper()}" for name in Settings.model_fields}
    nested = {f"MEDIA_TOOL_BROWSER__{name.upper()}" for name in BrowserSettings.model_fields}
    return top | nested


class TestEnvExample:
    def test_every_documented_variable_is_a_real_setting(self) -> None:
        # Settings reject unknown variables at startup, so a stale name here would send
        # someone off to debug a service that refuses to boot.
        unknown = [name for name in documented_variables() if name not in real_variables()]

        assert unknown == []

    def test_it_documents_a_meaningful_number_of_settings(self) -> None:
        assert len(documented_variables()) >= 20


class TestVersion:
    def test_the_declared_version_matches_the_package(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        assert pyproject["project"]["version"] == media_tool.__version__


class TestDocker:
    def test_the_image_pins_the_installed_playwright_version(self) -> None:
        # A mismatch between the image's bundled Chromium and the pinned Playwright is
        # the classic "works locally, fails in the container" failure.
        from importlib import metadata

        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        installed = metadata.version("playwright")

        assert f"playwright/python:v{installed}-" in dockerfile
