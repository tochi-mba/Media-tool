"""Sanity checks that the package is installed and self-consistent."""

from __future__ import annotations

import re
from importlib import metadata

import media_tool

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_is_semver() -> None:
    assert SEMVER.match(media_tool.__version__)


def test_declared_version_matches_installed_distribution() -> None:
    assert metadata.version("media-tool") == media_tool.__version__
