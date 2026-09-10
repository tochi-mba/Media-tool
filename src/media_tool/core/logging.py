"""Structured logging.

JSON in deployment so records are queryable, pretty console output locally. Every record
carries the request id automatically when one is bound, which is what makes a single
request traceable across the API, the job runner, and the browser adapter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import structlog

from media_tool.core.config import LogFormat
from media_tool.core.context import get_account, get_request_id

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger


def add_request_id(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the bound request id, if there is one.

    Absent outside a request rather than present-and-null, so queries can filter on
    existence.
    """
    request_id = get_request_id()
    if request_id is not None:
        event_dict["request_id"] = request_id
    return event_dict


def add_account(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the account the request was attributed to, if there is one.

    Every record produced while serving a request says whose request it was, which is
    what makes "why did this person's download fail" answerable without correlating by
    hand.
    """
    account = get_account()
    if account is not None:
        event_dict["account"] = str(account)
    return event_dict


def configure_logging(*, level: str, log_format: LogFormat) -> None:
    """Configure structlog process-wide. Safe to call again to change the configuration."""
    shared: list[Processor] = [
        structlog.processors.add_log_level,
        add_request_id,
        add_account,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if log_format is LogFormat.JSON
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> Any:
    """Return a logger tagged with ``name``.

    The name is bound into the event dict rather than read off the underlying logger, so
    it survives regardless of which logger factory is configured.

    The return type is deliberately loose: structlog's filtering bound loggers are
    generated at configuration time and have no single static type.
    """
    return structlog.get_logger().bind(logger=name)
