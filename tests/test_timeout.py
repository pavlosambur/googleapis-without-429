"""Tests for bounded waiting: timeouts and the non-blocking variant.

Without these the library can only ever block, which is right for a batch job
and wrong for anything serving a request: a stalled thread with nothing in the
log is indistinguishable from a hung process.
"""

from __future__ import annotations

import pytest

from googleapis_without_429 import SHEETS, QuotaLimiter, QuotaTimeoutError
from googleapis_without_429.core import WeightedSlidingWindow

from .conftest import FakeClock


def full_window(clock: FakeClock, limit: int = 2, window: float = 60.0):
    limiter = WeightedSlidingWindow(
        limit, window, name="test", clock=clock.time, sleeper=clock.sleep
    )
    for _ in range(limit):
        limiter.acquire()
    return limiter


class TestTimeout:
    def test_no_timeout_waits_as_long_as_needed(self, clock: FakeClock) -> None:
        limiter = full_window(clock)
        assert limiter.acquire() == pytest.approx(60.0)

    def test_a_timeout_shorter_than_the_wait_raises(self, clock: FakeClock) -> None:
        limiter = full_window(clock)
        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(timeout=5.0)

    def test_no_quota_is_consumed_when_the_wait_times_out(
        self, clock: FakeClock
    ) -> None:
        """A failed acquire must not eat quota the caller never got to use."""
        limiter = full_window(clock, limit=2)
        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(timeout=1.0)
        assert limiter.used == 2

    def test_it_never_sleeps_past_the_deadline(self, clock: FakeClock) -> None:
        """Waking up late would report a longer wait than was asked for."""
        limiter = full_window(clock)
        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(timeout=5.0)
        assert clock.now == pytest.approx(5.0)

    def test_a_timeout_longer_than_the_wait_succeeds(self, clock: FakeClock) -> None:
        limiter = full_window(clock)
        assert limiter.acquire(timeout=90.0) == pytest.approx(60.0)

    def test_the_error_carries_what_was_being_waited_for(
        self, clock: FakeClock
    ) -> None:
        limiter = full_window(clock, limit=2)
        with pytest.raises(QuotaTimeoutError) as caught:
            limiter.acquire(cost=2, timeout=3.0)

        error = caught.value
        assert error.name == "test"
        assert error.cost == 2
        assert error.timeout == 3.0
        assert error.waited == pytest.approx(3.0)
        assert "test" in str(error)

    def test_it_is_catchable_as_a_plain_timeout_error(self, clock: FakeClock) -> None:
        """Code that already handles timeouts should not need to know this library."""
        limiter = full_window(clock)
        with pytest.raises(TimeoutError):
            limiter.acquire(timeout=1.0)

    def test_a_negative_timeout_is_rejected(self, clock: FakeClock) -> None:
        limiter = full_window(clock)
        with pytest.raises(ValueError, match="timeout must not be negative"):
            limiter.acquire(timeout=-1.0)

    def test_a_zero_timeout_does_not_sleep_at_all(self, clock: FakeClock) -> None:
        limiter = full_window(clock)
        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(timeout=0)
        assert clock.slept == []


class TestTryAcquire:
    def test_it_succeeds_while_there_is_room(self, clock: FakeClock) -> None:
        limiter = WeightedSlidingWindow(2, 60.0, clock=clock.time, sleeper=clock.sleep)
        assert limiter.try_acquire() is True
        assert limiter.try_acquire() is True

    def test_it_returns_false_instead_of_waiting(self, clock: FakeClock) -> None:
        limiter = full_window(clock)
        assert limiter.try_acquire() is False
        assert clock.slept == []
        assert clock.now == 0.0

    def test_a_refusal_consumes_nothing(self, clock: FakeClock) -> None:
        limiter = full_window(clock, limit=2)
        limiter.try_acquire()
        assert limiter.used == 2

    def test_it_succeeds_again_once_the_window_reopens(self, clock: FakeClock) -> None:
        limiter = full_window(clock)
        assert limiter.try_acquire() is False
        clock.advance(60.0)
        assert limiter.try_acquire() is True


class TestThroughTheLimiter:
    def test_the_limiter_forwards_a_timeout(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1)],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        limiter.acquire(SHEETS, "read")

        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(SHEETS, "read", timeout=2.0)

    def test_the_limiter_offers_try_acquire(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1)],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        assert limiter.try_acquire(SHEETS, "read") is True
        assert limiter.try_acquire(SHEETS, "read") is False

    def test_acquire_for_accepts_a_timeout(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1)],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        url = "https://sheets.googleapis.com/v4/spreadsheets/abc"
        limiter.acquire_for("GET", url)

        with pytest.raises(QuotaTimeoutError):
            limiter.acquire_for("GET", url, timeout=1.0)

    def test_the_context_manager_accepts_a_timeout(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1)],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        limiter.acquire(SHEETS, "read")

        with (
            pytest.raises(QuotaTimeoutError),
            limiter.limit(SHEETS, "read", timeout=1.0),
        ):
            pass  # pragma: no cover - the wait raises before the body runs
