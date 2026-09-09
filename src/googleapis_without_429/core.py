"""The core rate limiting primitive: a thread-safe weighted sliding window."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from googleapis_without_429.errors import QuotaTimeoutError

__all__ = ["WeightedSlidingWindow", "WindowStats"]

logger = logging.getLogger(__name__)


@dataclass
class WindowStats:
    """What a window has done since it was created.

    A rate limiter that is working correctly looks exactly like a program that
    has hung, so some way to see the waiting is not a luxury. These counters
    cost nothing and need no configuration: read them from a health endpoint,
    log them at the end of a job, or check them in a debugger to find out
    whether a limit is set sensibly.

    Attributes:
        granted: Calls allowed through.
        waits: Calls that had to wait first.
        wait_seconds: Total time spent waiting.
        timeouts: Calls that gave up before quota freed up.
    """

    granted: int = 0
    waits: int = 0
    wait_seconds: float = 0.0
    timeouts: int = 0

    @property
    def average_wait(self) -> float:
        """Mean wait across the calls that waited, or ``0.0`` if none did."""
        return self.wait_seconds / self.waits if self.waits else 0.0


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
        #: Counters describing what this window has done. Updated under the
        #: lock, so they stay consistent when threads run in parallel.
        self.stats = WindowStats()

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

    def acquire(self, cost: int = 1, timeout: float | None = None) -> float:
        """Block until ``cost`` units fit in the window, then consume them.

        Args:
            cost: Weight of the call being made. Must be positive and no
                greater than ``limit`` -- a call that can never fit would
                otherwise block forever.
            timeout: Seconds to wait before giving up. ``None`` waits as long
                as it takes, which is right for a batch job and wrong for
                anything serving a request: a caller that would rather fail
                than stall for a minute needs to say so.

        Returns:
            Seconds spent waiting. ``0.0`` means the call passed straight
            through, which is the common case below the quota.

        Raises:
            ValueError: If ``cost`` is not positive, or exceeds ``limit``.
            QuotaTimeoutError: If ``timeout`` elapsed before room appeared. No
                quota is consumed in that case.
        """
        if cost <= 0:
            raise ValueError(f"cost must be positive, got {cost!r}")
        if cost > self.limit:
            label = f"{self.name}: " if self.name else ""
            raise ValueError(
                f"{label}cost {cost} exceeds limit {self.limit}; "
                f"this call can never be permitted"
            )

        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must not be negative, got {timeout!r}")

        started = self._clock()
        deadline = None if timeout is None else started + timeout
        waited_at_all = False
        while True:
            with self._lock:
                now = self._clock()
                self._expire(now)
                if self._used + cost <= self.limit:
                    self._log.append((now, cost))
                    self._used += cost
                    elapsed = now - started
                    self.stats.granted += 1
                    if waited_at_all:
                        self.stats.waits += 1
                        self.stats.wait_seconds += elapsed
                        logger.debug(
                            "%s: granted %d unit(s) after waiting %.3fs",
                            self.name or "window",
                            cost,
                            elapsed,
                        )
                    return elapsed
                sleep_for = self._time_until_room_for(cost, now)

            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    with self._lock:
                        self.stats.timeouts += 1
                    logger.debug(
                        "%s: gave up waiting for %d unit(s) after %.3fs",
                        self.name or "window",
                        cost,
                        timeout or 0.0,
                    )
                    raise QuotaTimeoutError(
                        self.name, cost, self._clock() - started, timeout or 0.0
                    )
                # Never sleep past the deadline: waking up late to raise a
                # timeout would report a longer wait than was asked for.
                sleep_for = min(sleep_for, remaining)

            if not waited_at_all:
                waited_at_all = True
                logger.debug(
                    "%s: quota exhausted, waiting %.3fs for %d unit(s)",
                    self.name or "window",
                    sleep_for,
                    cost,
                )

            # Sleep outside the lock. Holding it here would serialise every
            # other thread behind this one instead of rate limiting them.
            self._sleep(max(sleep_for, _MIN_SLEEP))

    def try_acquire(self, cost: int = 1) -> bool:
        """Consume ``cost`` units if they are available right now.

        Never blocks and never raises on a full window, which suits a caller
        that has something else to do meanwhile -- skip the item, queue it,
        serve a cached answer.

        Returns:
            ``True`` if the quota was consumed, ``False`` if the window is full.
        """
        try:
            self.acquire(cost, timeout=0)
        except QuotaTimeoutError:
            return False
        return True

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
