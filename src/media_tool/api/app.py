"""The application factory.

A factory rather than a module-level app: tests build an app per case with their own
settings, and nothing is constructed as a side effect of importing this module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from media_tool.api.errors import register_exception_handlers
from media_tool.api.middleware import AuthenticationMiddleware, RequestContextMiddleware
from media_tool.api.routers import ROUTERS
from media_tool.core.config import Settings, load_settings
from media_tool.core.container import Container
from media_tool.core.logging import configure_logging, get_logger
from media_tool.core.version import service_version

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = get_logger(__name__)

API_DESCRIPTION = """
Submit a list of media items and get a file for each one, fetched by an automated
headless browser.

Work is asynchronous: `create_download_job` accepts a batch and returns a job id
immediately, and `get_download_job` reports progress. That endpoint also long-polls --
pass `wait_seconds` and it returns as soon as the job finishes, so a caller does not
need a polling loop.
""".strip()


def create_app(
    settings: Settings | None = None,
    *,
    container_factory: Callable[[Settings], Container] = Container.build,
) -> FastAPI:
    """Build the application.

    Args:
        settings: configuration to use. Loaded from the environment when omitted, which
            is what the server entry point does; tests pass their own.
        container_factory: how to wire the dependencies at startup. The default is the
            real composition root; tests substitute one that serves keyring in-process.
    """
    settings = settings or load_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        description=API_DESCRIPTION,
        version=service_version(),
        lifespan=_lifespan,
        # Route summaries and operation ids are the contract an MCP bridge generates
        # tool names and descriptions from, so they are written for a model to read.
        openapi_tags=[
            {"name": "health", "description": "Liveness and dependency checks."},
            {"name": "downloads", "description": "Submit and track download jobs."},
        ],
    )
    app.state.settings = settings
    app.state.container_factory = container_factory

    # Added innermost first: Starlette makes the last-added middleware the outermost, so
    # this ordering puts the request context around authentication. A 401 gets a request
    # id, a log line and a timing header exactly like every other response.
    app.add_middleware(AuthenticationMiddleware)
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    for router in ROUTERS:
        app.include_router(router)

    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the container on startup and shut it down cleanly on the way out."""
    container = start(app)
    try:
        yield
    finally:
        await stop(container)


def start(app: FastAPI) -> Container:
    """Wire the application's dependencies and begin background retention."""
    factory: Callable[[Settings], Container] = app.state.container_factory
    container = factory(app.state.settings)
    app.state.container = container
    container.start_sweeper()

    logger.info(
        "service_started",
        provider=container.provider.name,
        artifact_dir=str(container.settings.artifact_dir),
        authentication=container.authenticator.describes,
    )
    return container


async def stop(container: Container) -> None:
    """Release everything the application holds open.

    Logged before closing rather than after, so a shutdown that hangs while draining
    still leaves a record of having been asked to stop.
    """
    logger.info("service_stopping")
    await container.aclose()
