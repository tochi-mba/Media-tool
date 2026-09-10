"""Request-scoped cross-cutting behaviour.

Two middlewares. :class:`RequestContextMiddleware` gives the request an id, makes that
id visible to every log record it produces, and records how long it took.
:class:`AuthenticationMiddleware` decides which account the request belongs to, once, at
the edge, so that nothing further in has to wonder.

Order matters and is set in the app factory: the context middleware wraps the
authentication one, so a rejected request still gets an id, a log line, and a timing
header like every other.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import status
from starlette.middleware.base import BaseHTTPMiddleware

from media_tool.api.errors import problem_response, unhandled_problem_response
from media_tool.core.context import bind_account, bind_request_id, new_request_id
from media_tool.core.logging import get_logger
from media_tool.domain.errors import AuthenticationError, KeyringUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
RESPONSE_TIME_HEADER = "X-Response-Time-Ms"

PUBLIC_PATHS = frozenset(
    {
        "/healthy",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)
"""The only paths that do not need an account.

An allowlist rather than a rule about ``/v1``: a route added tomorrow is protected by
default, and the mistake it makes possible -- a route that needs a token and does not
get asked for one -- is caught by the account dependency rather than served.

``/healthy`` is open so a load balancer can reach it, and the schema documents are open
so the interactive docs work in a browser. Both describe the service rather than anyone
using it, and a deployment that would rather not publish even that can block them at the
reverse proxy.
"""


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request id and logs the outcome of every request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # An id supplied by the caller is honoured so a trace can span services, but it
        # is length-capped: it ends up in every log record for this request.
        supplied = request.headers.get(REQUEST_ID_HEADER)
        request_id = supplied[:64] if supplied else new_request_id()

        started = time.perf_counter()
        with bind_request_id(request_id):
            try:
                response = await call_next(request)
            except Exception as exc:
                # Handled here rather than by Starlette's outermost error middleware,
                # which runs after this binding has unwound and would return a response
                # with no request id on it -- the one thing the caller is told to quote.
                logger.warning(
                    "request_failed",
                    method=request.method,
                    path=request.url.path,
                    duration_ms=_elapsed_ms(started),
                )
                response = unhandled_problem_response(exc)

            duration_ms = _elapsed_ms(started)
            logger.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                **_account_field(request),
            )

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[RESPONSE_TIME_HEADER] = f"{duration_ms:.1f}"
        return response


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _account_field(request: Request) -> dict[str, str]:
    """The account, for the completion record, or nothing on a public path.

    Read off the request rather than the context variable: this middleware wraps the
    authentication one, so by the time it logs, the binding that every record inside the
    request carried has already unwound.
    """
    account = getattr(request.state, "account", None)
    return {} if account is None else {"account": str(account)}


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Attributes every non-public request to an account, or refuses it."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        authenticator = request.app.state.container.authenticator
        try:
            account = await authenticator.account_for(request.headers.get("Authorization"))
        except AuthenticationError as error:
            # Deliberately not raised: an exception here would unwind past the app's own
            # handlers into the context middleware, which would render it as a 500.
            logger.info("request_unauthenticated", path=request.url.path)
            return problem_response(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(error),
                problem_type="unauthenticated",
            )
        except KeyringUnavailableError as error:
            # A 503, not a 401. The caller's token may be perfectly good; we cannot say.
            logger.warning("keyring_unavailable", path=request.url.path)
            return problem_response(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
                problem_type="keyring-unavailable",
            )

        # Both: the context variable for everything that runs inside the request --
        # handlers, and the job tasks they start -- and the request itself for the
        # completion record, which is emitted after this binding has unwound.
        request.state.account = account
        with bind_account(account):
            return await call_next(request)
