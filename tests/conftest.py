"""Shared test fixtures."""

from __future__ import annotations

import sys
import sysconfig

import pytest


def pytest_report_header() -> str:
    """State the GIL situation in the run header.

    The suite claims thread safety, and on a free-threaded build that claim
    is actually exercised. Printing it means a log shows which kind of run
    happened, rather than leaving it to be inferred from a job name.
    """
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        return "GIL: enabled (standard build)"
    enabled = sys._is_gil_enabled()  # type: ignore[attr-defined]
    return f"GIL: free-threaded build, currently {'enabled' if enabled else 'disabled'}"


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
