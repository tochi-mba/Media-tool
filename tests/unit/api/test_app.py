"""The application factory and its lifespan."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from media_tool.api.app import create_app, start, stop
from media_tool.api.routers import ROUTERS

if TYPE_CHECKING:
    from media_tool.core.config import Settings


def test_every_registered_router_is_published(settings: Settings) -> None:
    # The registry is the contract: anything listed in ROUTERS must appear in the
    # published schema, since that schema is what clients -- and an MCP bridge -- read.
    app = create_app(settings)
    published = set(app.openapi()["paths"])

    declared = {
        path
        for router in ROUTERS
        for route in router.routes
        if (path := getattr(route, "path", None))
    }

    assert declared
    assert declared <= published


@pytest.mark.usefixtures("keyring_env")
def test_the_factory_loads_settings_from_the_environment_when_given_none() -> None:
    app = create_app()

    assert app.state.settings is not None


def test_the_app_is_titled_and_versioned(settings: Settings) -> None:
    app = create_app(settings)

    assert app.title == settings.app_name
    assert app.version


async def test_start_wires_a_container_and_stop_releases_it(settings: Settings) -> None:
    app = create_app(settings)

    container = start(app)
    try:
        assert app.state.container is container
        assert container.provider.name == "stub"
    finally:
        await stop(container)

    assert container._sweeper is None
