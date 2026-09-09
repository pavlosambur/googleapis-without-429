"""Tests for the weighted sliding window."""

from __future__ import annotations

import threading
import time

import pytest

from googleapis_without_429.core import WeightedSlidingWindow

from .conftest import FakeClock


def make_window(clock: FakeClock, limit: int = 5, window: float = 60.0):
    return WeightedSlidingWindow(
        limit, window, name="test", clock=clock.time, sleeper=clock.sleep
    )


class TestValidation:
    def test_rejects_non_positive_limit(self, clock: FakeClock) -> None:
        with pytest.raises(ValueError, match="limit must be positive"):
            make_window(clock, limit=0)

    def test_rejects_non_positive_window(self, clock: FakeClock) -> None:
        with pytest.raises(ValueError, match="window must be positive"):
            make_window(clock, window=0)

    def test_cost_above_limit_raises_instead_of_blocking_forever(
        self, clock: FakeClock
    ) -> None:
        limiter = make_window(clock, limit=5)
        with pytest.raises(ValueError, match="can never be permitted"):
            limiter.acquire(cost=6)

    def test_rejects_non_positive_cost(self, clock: FakeClock) -> None:
        limiter = make_window(clock, limit=5)
        with pytest.raises(ValueError, match="cost must be positive"):
            limiter.acquire(cost=0)


class TestWindowBehaviour:
    def test_calls_within_the_limit_do_not_wait(self, clock: FakeClock) -> None:
        limiter = make_window(clock, limit=5)
        waits = [limiter.acquire() for _ in range(5)]
        assert waits == [0.0] * 5
        assert clock.slept == []

    def test_call_over_the_limit_waits_for_the_window_to_reopen(
        self, clock: FakeClock
    ) -> None:
        limiter = make_window(clock, limit=5, window=60.0)
        for _ in range(5):
            limiter.acquire()

        waited = limiter.acquire()

        assert waited == pytest.approx(60.0)
        assert clock.now == pytest.approx(60.0)

    def test_entries_expire_once_the_window_passes(self, clock: FakeClock) -> None:
        limiter = make_window(clock, limit=5, window=60.0)
        for _ in range(5):
            limiter.acquire()
        assert limiter.used == 5

        clock.advance(60.0)

        assert limiter.used == 0
        assert limiter.acquire() == 0.0

    def test_the_window_slides_rather_than_resetting(self, clock: FakeClock) -> None:
        """Capacity returns per call, not all at once when a fixed window flips."""
        limiter = make_window(clock, limit=2, window=10.0)
        limiter.acquire()  # at t=0
        clock.advance(4.0)
        limiter.acquire()  # at t=4

        # Only the t=0 entry has expired, so exactly one slot is free at t=10.
        assert limiter.acquire() == pytest.approx(6.0)
        assert limiter.used == 2


class TestWeightedCost:
    def test_cost_counts_as_weight_not_as_a_single_call(self, clock: FakeClock) -> None:
        """Three calls of cost 4 must not fit in a limit of 10, though 3 < 10."""
        limiter = make_window(clock, limit=10, window=60.0)
        limiter.acquire(cost=4)
        limiter.acquire(cost=4)

        assert limiter.used == 8
        assert limiter.acquire(cost=4) > 0.0

    def test_wait_releases_enough_weight_in_a_single_sleep(
        self, clock: FakeClock
    ) -> None:
        """Waiting for just the oldest entry would wake up with still no room.

        The log holds 2 units at t=0 and 8 units at t=1 against a limit of 10.
        A call of cost 9 needs 9 units released, which only happens once the
        t=1 entry expires. Sleeping until the *oldest* entry expires would wake
        the thread at t=10 with only 2 units freed, and it would sleep again.
        """
        limiter = make_window(clock, limit=10, window=10.0)
        limiter.acquire(cost=2)  # t=0
        clock.advance(1.0)
        limiter.acquire(cost=8)  # t=1

        clock.advance(1.0)  # t=2
        waited = limiter.acquire(cost=9)

        assert waited == pytest.approx(9.0)  # slept until t=11
        assert len(clock.slept) == 1, "woke up more than once: wait was miscomputed"


class TestThreadSafety:
    def test_concurrent_calls_never_exceed_the_limit_in_any_window(self) -> None:
        """The invariant that matters: no window ever holds more than the limit.

        This one uses the real clock -- a fake clock cannot model threads.
        """
        limit, window = 5, 0.1
        threads_count, per_thread = 6, 5
        limiter = WeightedSlidingWindow(limit, window, name="threads")

        stamps: list[float] = []
        stamps_lock = threading.Lock()

        def worker() -> None:
            for _ in range(per_thread):
                limiter.acquire()
                with stamps_lock:
                    stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(stamps) == threads_count * per_thread

        stamps.sort()
        for index, start in enumerate(stamps):
            in_window = sum(1 for other in stamps[index:] if other - start < window)
            assert in_window <= limit, (
                f"{in_window} calls within {window}s starting at index {index}, "
                f"limit is {limit}"
            )

    def test_concurrent_weighted_calls_never_exceed_the_limit(self) -> None:
        limit, window, cost = 10, 0.1, 2
        threads_count, per_thread = 5, 4
        limiter = WeightedSlidingWindow(limit, window, name="weighted-threads")

        stamps: list[float] = []
        stamps_lock = threading.Lock()

        def worker() -> None:
            for _ in range(per_thread):
                limiter.acquire(cost=cost)
                with stamps_lock:
                    stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        stamps.sort()
        for index, start in enumerate(stamps):
            weight = sum(cost for other in stamps[index:] if other - start < window)
            assert weight <= limit


def test_repr_shows_usage_against_the_limit(clock: FakeClock) -> None:
    limiter = make_window(clock, limit=5, window=60.0)
    limiter.acquire(cost=2)
    assert repr(limiter) == "<WeightedSlidingWindow 'test' 2/5 per 60.0s>"


def test_repr_without_a_name(clock: FakeClock) -> None:
    limiter = WeightedSlidingWindow(5, 60.0, clock=clock.time, sleeper=clock.sleep)
    assert repr(limiter) == "<WeightedSlidingWindow 0/5 per 60.0s>"


class TestUnderRealParallelism:
    """Heavier contention, which is what a free-threaded build actually tests.

    Under the GIL these threads interleave at bytecode boundaries; without it
    they run at the same instant on different cores, so a missing lock shows up
    here and nowhere else.
    """

    def test_mixed_costs_from_many_threads_stay_within_the_limit(self) -> None:
        limit, window = 20, 0.1
        costs = [1, 2, 3, 5]
        threads_count, per_thread = 10, 8
        limiter = WeightedSlidingWindow(limit, window, name="parallel")

        charged: list[tuple[float, int]] = []
        charged_lock = threading.Lock()

        def worker(index: int) -> None:
            cost = costs[index % len(costs)]
            for _ in range(per_thread):
                limiter.acquire(cost=cost)
                with charged_lock:
                    charged.append((time.monotonic(), cost))

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(threads_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(charged) == threads_count * per_thread

        charged.sort()
        for index, (start, _) in enumerate(charged):
            weight = sum(
                cost for moment, cost in charged[index:] if moment - start < window
            )
            assert weight <= limit, f"{weight} units within {window}s, limit {limit}"

    def test_the_internal_ledger_stays_consistent_under_contention(self) -> None:
        """`used` must never drift from what the log actually holds."""
        limiter = WeightedSlidingWindow(50, 0.05, name="ledger")

        def worker() -> None:
            for _ in range(20):
                limiter.acquire(cost=2)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with limiter._lock:
            assert limiter._used == sum(cost for _, cost in limiter._log)
            assert limiter._used >= 0
