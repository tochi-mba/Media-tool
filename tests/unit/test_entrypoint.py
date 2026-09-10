"""The server entry point."""

from __future__ import annotations

from typing import Any

import pytest
import uvicorn

import media_tool.__main__ as entrypoint


@pytest.mark.usefixtures("keyring_env")
def test_main_serves_the_app_factory_on_the_configured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(target: str, **kwargs: Any) -> None:
        captured["target"] = target
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("MEDIA_TOOL_PORT", "9123")
    monkeypatch.setenv("MEDIA_TOOL_HOST", "0.0.0.0")

    entrypoint.main()

    assert captured["target"] == "media_tool.api.app:create_app"
    assert captured["factory"] is True
    assert captured["port"] == 9123
    assert captured["host"] == "0.0.0.0"
