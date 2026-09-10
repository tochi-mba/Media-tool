"""Version resolution."""

from __future__ import annotations

from importlib import metadata

import pytest

import media_tool
from media_tool.core.version import service_version


def test_it_reports_the_installed_version() -> None:
    assert service_version() == media_tool.__version__


def test_it_falls_back_to_the_source_constant_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_installed(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", not_installed)

    assert service_version() == media_tool.__version__
