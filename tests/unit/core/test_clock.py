"""The clock is injected everywhere time matters, so tests never sleep or race."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from media_tool.core.clock import Clock, SystemClock
from tests.fakes.clock import FakeClock


def test_system_clock_returns_timezone_aware_utc() -> None:
    now = SystemClock().now()

    assert now.tzinfo is UTC


def test_system_clock_advances() -> None:
    clock = SystemClock()

    first = clock.monotonic()
    second = clock.monotonic()

    assert second >= first


def test_system_clock_satisfies_the_clock_protocol() -> None:
    clock: Clock = SystemClock()

    assert isinstance(clock.now(), datetime)
    assert isinstance(clock.monotonic(), float)


def test_fake_clock_is_frozen_until_advanced() -> None:
    clock = FakeClock()

    start = clock.now()
    assert clock.now() == start

    clock.advance(timedelta(seconds=30))

    assert clock.now() == start + timedelta(seconds=30)
    assert clock.monotonic() == 30.0
