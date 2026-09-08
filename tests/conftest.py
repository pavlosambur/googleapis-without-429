"""Shared test fixtures."""

from __future__ import annotations

import pytest


class FakeClock:
    """A deterministic stand-in for ``time.monotonic`` plus ``time.sleep``.

    Sleeping moves the clock forward instead of blocking, so timing tests run
    instantly and cannot flake on a loaded CI machine. ``slept`` records every
    sleep, which lets a test assert *how many times* a thread woke up, not just
    how long it waited in total.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
