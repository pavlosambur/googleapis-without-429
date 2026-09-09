"""Tests for the asynchronous path.

The rule being checked is that there is only one rule: `acquire` and
`acquire_async` differ in how they wait and in nothing else, so a window shared
between threads and coroutines meters them against one quota.
"""

from __future__ import annotations

import asyncio

import pytest

from googleapis_without_429 import SHEETS, QuotaLimiter, QuotaTimeoutError
from googleapis_without_429.core import WeightedSlidingWindow

from .conftest import FakeClock


def window(clock: FakeClock, limit: int = 2, length: float = 60.0):
    return WeightedSlidingWindow(
        limit,
        length,
        name="async-test",
        clock=clock.time,
        sleeper=clock.sleep,
        async_sleeper=clock.async_sleep,
    )


class TestAcquireAsync:
    async def test_calls_within_the_quota_do_not_wait(self, clock: FakeClock) -> None:
        limiter = window(clock)

        assert await limiter.acquire_async() == 0.0
        assert await limiter.acquire_async() == 0.0
        assert clock.slept == []

    async def test_a_call_over_the_quota_waits(self, clock: FakeClock) -> None:
        limiter = window(clock)
        await limiter.acquire_async()
        await limiter.acquire_async()

        assert await limiter.acquire_async() == pytest.approx(60.0)

    async def test_weighted_costs_are_honoured(self, clock: FakeClock) -> None:
        limiter = window(clock, limit=10)
        await limiter.acquire_async(cost=4)
        await limiter.acquire_async(cost=4)

        assert limiter.used == 8
        assert await limiter.acquire_async(cost=4) > 0.0

    async def test_a_timeout_raises_rather_than_stalling(
        self, clock: FakeClock
    ) -> None:
        limiter = window(clock)
        await limiter.acquire_async()
        await limiter.acquire_async()

        with pytest.raises(QuotaTimeoutError):
            await limiter.acquire_async(timeout=5.0)
        assert clock.now == pytest.approx(5.0)

    async def test_a_timed_out_call_consumes_nothing(self, clock: FakeClock) -> None:
        limiter = window(clock)
        await limiter.acquire_async()
        await limiter.acquire_async()

        with pytest.raises(QuotaTimeoutError):
            await limiter.acquire_async(timeout=1.0)
        assert limiter.used == 2

    async def test_a_cost_that_can_never_fit_is_rejected(
        self, clock: FakeClock
    ) -> None:
        limiter = window(clock, limit=5)
        with pytest.raises(ValueError, match="can never be permitted"):
            await limiter.acquire_async(cost=6)

    async def test_waiting_is_counted_in_the_same_stats(self, clock: FakeClock) -> None:
        limiter = window(clock, limit=1)
        await limiter.acquire_async()
        await limiter.acquire_async()

        assert limiter.stats.granted == 2
        assert limiter.stats.waits == 1
        assert limiter.stats.wait_seconds == pytest.approx(60.0)


class TestOneQuotaForBothWorlds:
    async def test_a_sync_call_and_an_async_call_share_the_window(
        self, clock: FakeClock
    ) -> None:
        """The point of sharing the decision: one quota, two ways of waiting."""
        limiter = window(clock, limit=2)

        limiter.acquire()
        await limiter.acquire_async()

        assert limiter.used == 2
        assert await limiter.acquire_async() == pytest.approx(60.0)

    async def test_a_queued_async_caller_is_released_on_timeout(
        self, clock: FakeClock
    ) -> None:
        """An abandoned ticket at the front would block everyone behind it."""
        limiter = window(clock, limit=1)
        await limiter.acquire_async()

        with pytest.raises(QuotaTimeoutError):
            await limiter.acquire_async(timeout=5.0)

        assert list(limiter._waiting) == []
        clock.advance(60.0)
        assert await limiter.acquire_async() == 0.0


class TestRealConcurrency:
    """With the real event loop, since a fake clock cannot model interleaving."""

    async def test_coroutines_share_one_quota(self) -> None:
        limiter = WeightedSlidingWindow(4, 0.1, name="gathered")

        async def call(index: int) -> int:
            await limiter.acquire_async()
            return index

        results = await asyncio.gather(*(call(i) for i in range(4)))

        assert sorted(results) == [0, 1, 2, 3]
        assert limiter.used == 4

    async def test_the_quota_actually_paces_the_gather(self) -> None:
        """Eight calls against a limit of four take at least one extra window."""
        limit, length = 4, 0.1
        limiter = WeightedSlidingWindow(limit, length, name="paced")

        started = asyncio.get_running_loop().time()
        await asyncio.gather(*(limiter.acquire_async() for _ in range(8)))
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= length, "the second batch did not wait for the window"

    async def test_the_event_loop_is_not_blocked_while_waiting(self) -> None:
        """A blocking sleep here would stall everything else on the loop."""
        limiter = WeightedSlidingWindow(1, 0.2, name="non-blocking")
        await limiter.acquire_async()

        ticks = 0

        async def busy() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        spinner = asyncio.create_task(busy())
        try:
            await limiter.acquire_async()
        finally:
            spinner.cancel()

        assert ticks > 3, f"the loop only ran {ticks} times while the limiter waited"


class TestLimitAsyncContextManager:
    async def test_it_waits_then_runs_the_block(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1)],
            clock=clock.time,
            sleeper=clock.sleep,
            async_sleeper=clock.async_sleep,
        )
        await limiter.acquire_async(SHEETS, "read")

        async with limiter.limit_async(SHEETS, "read") as waited:
            assert waited == pytest.approx(60.0)

        assert limiter.bucket(SHEETS, "read").used == 1

    async def test_quota_is_spent_even_if_the_block_raises(
        self, clock: FakeClock
    ) -> None:
        limiter = QuotaLimiter(
            [SHEETS],
            clock=clock.time,
            sleeper=clock.sleep,
            async_sleeper=clock.async_sleep,
        )

        with pytest.raises(RuntimeError):
            async with limiter.limit_async(SHEETS, "write"):
                raise RuntimeError("the call failed")

        assert limiter.bucket(SHEETS, "write").used == 1
