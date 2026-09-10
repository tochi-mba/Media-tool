"""Translating exceptions into RFC 9457 problem responses.

This is the only place in the service that maps a failure to a status code. Handlers
stay thin because of it: they raise domain errors and let this decide what that means
over HTTP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from media_tool.api.schemas.common import PROBLEM_CONTENT_TYPE, FieldError, Problem
from media_tool.core.context import get_request_id
from media_tool.core.logging import get_logger
from media_tool.domain.errors import (
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    InvalidJobTransitionError,
    InvalidMediaQueryError,
    JobItemNotFoundError,
    JobNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

logger = get_logger(__name__)

PROBLEM_BASE_URI = "https://media-tool.invalid/problems"

_STATUS_TITLES = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_410_GONE: "Gone",
    status.HTTP_413_CONTENT_TOO_LARGE: "Payload too large",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation failed",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
}

# Domain errors that map cleanly onto a status code. Anything absent is a bug and
# becomes a 500 with its detail withheld.
_DOMAIN_STATUS = {
    JobNotFoundError: status.HTTP_404_NOT_FOUND,
    JobItemNotFoundError: status.HTTP_404_NOT_FOUND,
    ArtifactNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidJobTransitionError: status.HTTP_409_CONFLICT,
    InvalidMediaQueryError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ArtifactTooLargeError: status.HTTP_413_CONTENT_TOO_LARGE,
}


def problem_response(
    *,
    status_code: int,
    detail: str,
    problem_type: str | None = None,
    title: str | None = None,
    errors: list[FieldError] | None = None,
) -> JSONResponse:
    """Build a problem+json response carrying the current request id."""
    slug = problem_type or _slug_for(status_code)
    problem = Problem(
        type=f"{PROBLEM_BASE_URI}/{slug}",
        title=title or _STATUS_TITLES.get(status_code, "Error"),
        status=status_code,
        detail=detail,
        request_id=get_request_id(),
        errors=errors,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def _slug_for(status_code: int) -> str:
    return _STATUS_TITLES.get(status_code, "error").lower().replace(" ", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler the app needs. Called once, by the app factory."""

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Reshape FastAPI's validation errors into the one error format this API uses."""
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the request body failed validation",
            problem_type="validation-failed",
            errors=[
                FieldError(
                    location=".".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                )
                for error in exc.errors()
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(status_code=exc.status_code, detail=str(exc.detail))

    for error_type, status_code in _DOMAIN_STATUS.items():
        app.add_exception_handler(error_type, _domain_handler(status_code))


def unhandled_problem_response(exc: BaseException) -> JSONResponse:
    """Render an unexpected exception as a 500.

    The exception's own message is withheld: it can carry filesystem paths, internal
    hostnames, or credentials. The request id ties the response to the log record that
    does have the detail -- which is why this is invoked from inside
    :class:`~media_tool.api.middleware.RequestContextMiddleware`, while the id is still
    bound, rather than from Starlette's outermost error middleware, where the binding
    has already unwound and the response would carry no id at all.
    """
    logger.exception("unhandled_exception", error_type=type(exc).__name__)
    return problem_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="an unexpected error occurred; quote the request id when reporting it",
    )


def _domain_handler(
    status_code: int,
) -> Callable[[Request, Exception], Coroutine[Any, Any, JSONResponse]]:
    """Build a handler that renders a domain error at ``status_code``."""

    async def handler(_request: Request, exc: Exception) -> JSONResponse:
        return problem_response(status_code=status_code, detail=str(exc))

    return handler
