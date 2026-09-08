"""The core rate limiting primitive: a thread-safe weighted sliding window."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

__all__ = ["WeightedSlidingWindow"]

# Never busy-spin: even when the computed wait rounds down to zero, yield for a
# moment so other threads make progress.
_MIN_SLEEP = 0.001


class WeightedSlidingWindow:
    """Allow at most ``limit`` units of cost within any ``window`` seconds.

    Every acquisition carries a *cost*. Google APIs differ in what they count:
    Sheets counts requests (cost 1 each), while Gmail counts quota units, where
    a single ``messages.send`` costs 100. Counting weight rather than calls
    keeps one implementation correct for both.

    The window slides over the timestamps of our own calls, which makes it
    strictly more conservative than the fixed windows Google actually uses --
    we never allow more than the limit, but we may allow less. See the README
    on why this does not remove the need to retry on 429.

    Instances are safe to share between threads.

    Args:
        limit: Maximum total cost permitted within one window.
        window: Length of the window, in seconds.
        name: Optional label, used only in :func:`repr` and error messages.
        clock: Monotonic time source. Injectable for testing.
        sleeper: Blocking sleep function. Injectable for testing.
    """

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        name: str = "",
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit!r}")
        if window <= 0:
            raise ValueError(f"window must be positive, got {window!r}")

        self.limit = limit
        self.window = window
        self.name = name

        self._clock = clock
        self._sleep = sleeper
        # (timestamp, cost) pairs, oldest first.
        self._log: deque[tuple[float, int]] = deque()
        self._used = 0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        label = f"{self.name!r} " if self.name else ""
        return (
            f"<{type(self).__name__} {label}"
            f"{self._used}/{self.limit} per {self.window}s>"
        )

    @property
    def used(self) -> int:
        """Cost currently counted against the window, after expiring old entries."""
        with self._lock:
            self._expire(self._clock())
            return self._used

    def acquire(self, cost: int = 1) -> float:
        """Block until ``cost`` units fit in the window, then consume them.

        Args:
            cost: Weight of the call being made. Must be positive and no
                greater than ``limit`` -- a call that can never fit would
                otherwise block forever.

        Returns:
            Seconds spent waiting. ``0.0`` means the call passed straight
            through, which is the common case below the quota.

        Raises:
            ValueError: If ``cost`` is not positive, or exceeds ``limit``.
        """
        if cost <= 0:
            raise ValueError(f"cost must be positive, got {cost!r}")
        if cost > self.limit:
            label = f"{self.name}: " if self.name else ""
            raise ValueError(
                f"{label}cost {cost} exceeds limit {self.limit}; "
                f"this call can never be permitted"
            )

        started = self._clock()
        while True:
            with self._lock:
                now = self._clock()
                self._expire(now)
                if self._used + cost <= self.limit:
                    self._log.append((now, cost))
                    self._used += cost
                    return now - started
                sleep_for = self._time_until_room_for(cost, now)
            # Sleep outside the lock. Holding it here would serialise every
            # other thread behind this one instead of rate limiting them.
            self._sleep(max(sleep_for, _MIN_SLEEP))

    def _expire(self, now: float) -> None:
        """Drop entries that have fallen out of the window. Caller holds the lock."""
        while self._log and now - self._log[0][0] >= self.window:
            self._used -= self._log.popleft()[1]

    def _time_until_room_for(self, cost: int, now: float) -> float:
        """Seconds until enough entries expire to fit ``cost``. Caller holds the lock.

        Waiting only for the oldest entry would be wrong for weighted costs:
        expiring one entry worth 2 does not make room for a call worth 100, and
        the thread would wake up only to sleep again. Walk the log until enough
        weight has been released, and sleep until that entry expires.
        """
        needed = self._used + cost - self.limit
        released = 0
        for timestamp, entry_cost in self._log:
            released += entry_cost
            if released >= needed:
                return self.window - (now - timestamp)
        # Unreachable: cost <= limit guarantees the whole log releases enough.
        raise AssertionError(  # pragma: no cover
            "window log does not hold enough cost to release"
        )
