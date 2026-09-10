"""Per-request context.

A request id and an account are attached once, at the edge, and read wherever they are
needed -- log records, error responses -- without being passed through every signature.
Context variables are task-local, so concurrent requests never see each other's.

The account is here for observability, not for authorization. Anything that acts on an
account takes one as an argument: a store method that reached into a context variable
for it would be a store method that behaves differently depending on who called it, and
impossible to reason about from its signature.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from media_tool.domain.accounts import AccountId

_request_id: ContextVar[str | None] = ContextVar("media_tool_request_id", default=None)
_account: ContextVar[AccountId | None] = ContextVar("media_tool_account", default=None)


def new_request_id() -> str:
    """Return a fresh request id."""
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    """Return the current request id, or ``None`` outside a request."""
    return _request_id.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[str]:
    """Bind ``request_id`` for the duration of the block, restoring the previous value after."""
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _request_id.reset(token)


def get_account() -> AccountId | None:
    """Return the account this request was attributed to, or ``None`` outside one."""
    return _account.get()


@contextmanager
def bind_account(account: AccountId) -> Iterator[AccountId]:
    """Bind ``account`` for the duration of the block, restoring the previous value after."""
    token = _account.set(account)
    try:
        yield account
    finally:
        _account.reset(token)
