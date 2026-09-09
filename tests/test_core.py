"""Tests for the weighted sliding window."""

from __future__ import annotations

import threading
import time
from collections import deque

import pytest

from googleapis_without_429.core import WeightedSlidingWindow
from googleapis_without_429.errors import QuotaTimeoutError

from .conftest import FakeClock


def make_window(clock: FakeClock, limit: int = 5, window: float = 60.0):
    return WeightedSlidingWindow(
        limit, window, name="test", clock=clock.time, sleeper=clock.sleep
    )


class _RecordingLog(deque):  # type: ignore[type-arg]
    """A deque that copies every appended entry to a sink.

    The limiter appends ``(timestamp, cost)`` under its own lock, immediately
    after reading the clock, so entries collected here are the exact values the
    window is reasoning about.
    """

    def __init__(self, sink: list[tuple[float, int]]) -> None:
        super().__init__()
        self._sink = sink

    def append(self, item: tuple[float, int]) -> None:
        self._sink.append(item)
        super().append(item)


class RecordingWindow(WeightedSlidingWindow):
    """A window that remembers every charge it ever granted.

    Timing these calls from the outside cannot work: the quota is charged
    inside acquire, and the scheduler may preempt the thread at any point
    between that and the test's own clock read. On a loaded CI runner the drift
    is large enough to make calls from different windows look simultaneous,
    which produced a test that failed roughly one run in five for no reason at
    all. Recording the limiter's own timestamps removes the guesswork.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.charges: list[tuple[float, int]] = []
        self._log = _RecordingLog(self.charges)


def assert_within_limit(
    charges: list[tuple[float, int]], limit: int, window: float
) -> None:
    """No window of `window` seconds may hold more than `limit` units."""
    entries = sorted(charges)
    for index, (start, _) in enumerate(entries):
        weight = sum(
            cost for moment, cost in entries[index:] if moment - start < window
        )
        assert weight <= limit, (
            f"{weight} units within {window}s starting at index {index}, "
            f"limit is {limit}"
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
        """The invariant that matters: no window ever holds more than the limit."""
        limit, window = 5, 0.1
        threads_count, per_thread = 6, 5
        limiter = RecordingWindow(limit, window, name="threads")

        def worker() -> None:
            for _ in range(per_thread):
                limiter.acquire()

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(limiter.charges) == threads_count * per_thread
        assert_within_limit(limiter.charges, limit, window)

    def test_concurrent_weighted_calls_never_exceed_the_limit(self) -> None:
        limit, window, cost = 10, 0.1, 2
        threads_count, per_thread = 5, 4
        limiter = RecordingWindow(limit, window, name="weighted-threads")

        def worker() -> None:
            for _ in range(per_thread):
                limiter.acquire(cost=cost)

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert_within_limit(limiter.charges, limit, window)


class TestUnderRealParallelism:
    """Heavier contention, which is what a free-threaded build actually tests.

    Under the GIL these threads interleave at bytecode boundaries; without it
    they run at the same instant on different cores, so a missing lock shows up
    here and nowhere else.
    """

    def test_mixed_costs_from_many_threads_stay_within_the_limit(self) -> None:
        limit, window = 20, 0.1
        costs = [1, 2, 3, 5]
        threads_count, per_thread = 8, 4
        limiter = RecordingWindow(limit, window, name="parallel")

        def worker(index: int) -> None:
            cost = costs[index % len(costs)]
            for _ in range(per_thread):
                limiter.acquire(cost=cost)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(threads_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(limiter.charges) == threads_count * per_thread
        assert_within_limit(limiter.charges, limit, window)

    def test_total_throughput_cannot_beat_the_quota(self) -> None:
        """A second check, from the outside, that needs no per-call precision.

        However the calls interleave, a sliding window can only let through
        `limit` units per window plus one window's worth already in flight.
        Elapsed time measured from outside can only be an overestimate, which
        makes the bound generous rather than flaky.
        """
        limit, window, cost = 6, 0.1, 3
        threads_count, per_thread = 4, 3
        limiter = WeightedSlidingWindow(limit, window, name="throughput")

        def worker() -> None:
            for _ in range(per_thread):
                limiter.acquire(cost=cost)

        started = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - started

        granted = threads_count * per_thread * cost
        allowed = limit * (elapsed / window + 1)
        assert granted <= allowed, (
            f"{granted} units in {elapsed:.3f}s exceeds the {allowed:.1f} "
            f"a limit of {limit} per {window}s can allow"
        )

    def test_the_internal_ledger_stays_consistent_under_contention(self) -> None:
        """`used` must never drift from what the log actually holds."""
        limiter = WeightedSlidingWindow(50, 0.05, name="ledger")

        def worker() -> None:
            for _ in range(15):
                limiter.acquire(cost=2)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with limiter._lock:
            assert limiter._used == sum(cost for _, cost in limiter._log)
            assert limiter._used >= 0


def test_repr_shows_usage_against_the_limit(clock: FakeClock) -> None:
    limiter = make_window(clock, limit=5, window=60.0)
    limiter.acquire(cost=2)
    assert repr(limiter) == "<WeightedSlidingWindow 'test' 2/5 per 60.0s>"


def test_repr_without_a_name(clock: FakeClock) -> None:
    limiter = WeightedSlidingWindow(5, 60.0, clock=clock.time, sleeper=clock.sleep)
    assert repr(limiter) == "<WeightedSlidingWindow 0/5 per 60.0s>"


class TestFairness:
    """An expensive call must not lose every race to cheap ones.

    Drive prices a download at 200 units and a metadata read at 5. Without an
    ordered queue, cheap calls keep the window just full enough that the
    expensive one never fits, and it waits forever while everything else
    proceeds.
    """

    def test_a_cheap_call_cannot_overtake_an_expensive_one(self) -> None:
        """The precise shape of starvation: quota freeing up in pieces.

        Ten units are consumed in two batches half a window apart, so capacity
        returns in two instalments of five. A call needing all ten must wait
        for both; a call needing one fits after the first. Without a queue the
        cheap call takes that opening every time, and repeated cheap calls mean
        the expensive one never runs.
        """
        window = 0.3
        limiter = WeightedSlidingWindow(10, window, name="overtaking")
        for _ in range(5):
            limiter.acquire()
        time.sleep(window / 2)
        for _ in range(5):
            limiter.acquire()

        finished: list[str] = []

        def expensive() -> None:
            limiter.acquire(cost=10)
            finished.append("expensive")

        def cheap() -> None:
            limiter.acquire(cost=1)
            finished.append("cheap")

        first = threading.Thread(target=expensive)
        first.start()
        # Let the expensive caller reach the queue before the cheap one asks,
        # otherwise the test is about arrival order rather than fairness.
        while not limiter._waiting:
            time.sleep(0.001)

        second = threading.Thread(target=cheap)
        second.start()
        first.join(timeout=5.0)
        second.join(timeout=5.0)

        assert finished == ["expensive", "cheap"], (
            "the cheap call took the first opening and jumped the queue"
        )

    def test_an_expensive_call_is_not_starved_by_a_stream_of_cheap_ones(
        self,
    ) -> None:
        """The same thing under continuous pressure rather than one overtake."""
        limiter = WeightedSlidingWindow(10, 0.1, name="fairness")
        stop = threading.Event()

        def cheap() -> None:
            while not stop.is_set():
                limiter.acquire(cost=1)

        threads = [threading.Thread(target=cheap, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            waited = limiter.acquire(cost=10, timeout=5.0)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2.0)

        assert waited < 5.0

    def test_callers_are_served_in_the_order_they_arrived(
        self, clock: FakeClock
    ) -> None:
        """Single-threaded proof that the queue is consulted at all."""
        limiter = WeightedSlidingWindow(
            1, 60.0, name="order", clock=clock.time, sleeper=clock.sleep
        )
        limiter.acquire()

        # A waiter joins the queue by attempting and timing out; the queue must
        # be empty again afterwards so it cannot block anyone.
        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(timeout=1.0)
        assert list(limiter._waiting) == []

    def test_a_caller_that_gives_up_does_not_block_the_queue(
        self, clock: FakeClock
    ) -> None:
        """A departed waiter left in the queue would stall everyone behind it."""
        limiter = WeightedSlidingWindow(
            1, 60.0, name="departed", clock=clock.time, sleeper=clock.sleep
        )
        limiter.acquire()

        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(timeout=5.0)

        clock.advance(60.0)
        assert limiter.acquire() == 0.0, "the next caller was not blocked"

    def test_the_queue_is_empty_when_nothing_is_waiting(self, clock: FakeClock) -> None:
        limiter = WeightedSlidingWindow(5, 60.0, clock=clock.time, sleeper=clock.sleep)
        for _ in range(5):
            limiter.acquire()
        assert list(limiter._waiting) == []

    def test_dropping_a_ticket_that_is_not_queued_is_harmless(
        self, clock: FakeClock
    ) -> None:
        """Defensive: the cleanup path must not raise on a double removal."""
        limiter = WeightedSlidingWindow(1, 60.0, clock=clock.time, sleeper=clock.sleep)
        limiter._waiting.append((7, 1))

        limiter._drop_ticket(99)

        assert list(limiter._waiting) == [(7, 1)]

    def test_an_empty_queue_yields_the_minimum_sleep(self, clock: FakeClock) -> None:
        """Defensive: never return zero, which would spin."""
        limiter = WeightedSlidingWindow(1, 60.0, clock=clock.time, sleeper=clock.sleep)
        assert limiter._time_until_the_front_can_go(clock.time()) > 0
