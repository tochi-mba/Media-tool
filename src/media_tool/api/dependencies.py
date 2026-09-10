"""FastAPI dependency wiring.

The container is built once at startup and parked on the app; these turn it into typed
parameters so handlers never reach into application state themselves.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from media_tool.core.container import Container
from media_tool.core.context import get_account
from media_tool.core.keyring.authenticator import MISSING_TOKEN
from media_tool.domain.accounts import AccountId
from media_tool.domain.errors import AuthenticationError


def get_container(request: Request) -> Container:
    """Return the container assembled during startup."""
    container: Container = request.app.state.container
    return container


def current_account() -> AccountId:
    """Return the account this request was attributed to.

    The authentication middleware has already run for every path that is not in
    :data:`~media_tool.api.middleware.PUBLIC_PATHS`, so in practice this always finds
    one. It refuses rather than assuming when it does not, which is what turns
    "somebody made a protected route public" from a data leak into a 401, in the same
    words a missing token gets.

    Raises:
        AuthenticationError: no account is bound to this request.
    """
    account = get_account()
    if account is None:
        raise AuthenticationError(MISSING_TOKEN)
    return account


ContainerDep = Annotated[Container, Depends(get_container)]
AccountDep = Annotated[AccountId, Depends(current_account)]
