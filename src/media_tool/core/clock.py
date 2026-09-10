"""Time as an injectable dependency.

Nothing in this codebase calls :func:`datetime.now` or :func:`time.monotonic` directly.
Every component that needs the time takes a :class:`Clock`, so tests control it exactly
and never depend on wall-clock timing.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Reads the current time.

    Two readings are exposed because they answer different questions: :meth:`now` is a
    timestamp fit to record and serialize, while :meth:`monotonic` measures elapsed
    duration and is immune to system clock adjustments.
    """

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonically increasing number of seconds from an arbitrary origin."""
        ...


class SystemClock:
    """The real clock, used everywhere outside tests."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
