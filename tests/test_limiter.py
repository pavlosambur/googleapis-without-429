"""Tests for the standalone limiter: the API for code that has no session."""

from __future__ import annotations

import logging

import pytest

from googleapis_without_429 import (
    DRIVE,
    SHEETS,
    ApiProfile,
    QuotaLimiter,
    QuotaTimeoutError,
)
from googleapis_without_429.profiles import resolve_sheets

from .conftest import FakeClock

SHEET_URL = "https://sheets.googleapis.com/v4/spreadsheets/abc123"


def make_limiter(clock: FakeClock, **limits: int) -> QuotaLimiter:
    profile = SHEETS.with_limits(**limits) if limits else SHEETS
    return QuotaLimiter([profile], clock=clock.time, sleeper=clock.sleep)


class TestConstruction:
    def test_rejects_an_empty_profile_list(self) -> None:
        with pytest.raises(ValueError, match="at least one profile"):
            QuotaLimiter([])

    def test_rejects_duplicate_profile_names(self) -> None:
        with pytest.raises(ValueError, match="must be unique"):
            QuotaLimiter([SHEETS, SHEETS])

    def test_repr_names_its_profiles(self) -> None:
        assert repr(QuotaLimiter([SHEETS, DRIVE])) == "<QuotaLimiter [sheets, drive]>"


class TestBucketAccess:
    def test_a_bucket_reports_what_it_has_consumed(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        limiter.acquire(SHEETS, "write")

        assert limiter.bucket(SHEETS, "write").used == 1

    def test_an_unmetered_profile_is_named_in_the_error(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        with pytest.raises(ValueError, match="'drive' is not metered"):
            limiter.bucket(DRIVE, "units")

    def test_an_unknown_bucket_lists_the_real_ones(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        with pytest.raises(ValueError, match="unknown bucket 'writes'"):
            limiter.bucket(SHEETS, "writes")


class TestAcquireForUrl:
    def test_a_url_is_classified_by_its_profile(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter([SHEETS, DRIVE], clock=clock.time, sleeper=clock.sleep)

        limiter.acquire_for("GET", SHEET_URL)
        limiter.acquire_for("GET", "https://www.googleapis.com/drive/v3/files")

        assert limiter.bucket(SHEETS, "read").used == 1
        assert limiter.bucket(DRIVE, "units").used == 100

    def test_an_unclaimed_url_costs_nothing(self, clock: FakeClock) -> None:
        """Token refreshes and unrelated hosts must not consume a quota."""
        limiter = make_limiter(clock, read=1, write=1)

        token_url = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
        for _ in range(5):
            assert limiter.acquire_for("POST", token_url) == 0.0

        assert limiter.bucket(SHEETS, "read").used == 0
        assert limiter.bucket(SHEETS, "write").used == 0

    def test_waiting_is_reported_in_seconds(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, read=1, write=60)

        assert limiter.acquire_for("GET", SHEET_URL) == 0.0
        assert limiter.acquire_for("GET", SHEET_URL) == pytest.approx(60.0)


class TestLimitAsContextManager:
    def test_the_block_runs_after_the_wait(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, read=60, write=1)
        limiter.acquire(SHEETS, "write")

        with limiter.limit(SHEETS, "write") as waited:
            assert waited == pytest.approx(60.0)

        assert clock.now == pytest.approx(60.0)

    def test_quota_is_consumed_even_if_the_block_raises(self, clock: FakeClock) -> None:
        """The request may already be in flight; the quota is spent regardless."""
        limiter = make_limiter(clock)

        with pytest.raises(RuntimeError), limiter.limit(SHEETS, "write"):
            raise RuntimeError("the call failed")

        assert limiter.bucket(SHEETS, "write").used == 1


class TestLimitAsDecorator:
    def test_each_call_of_the_decorated_function_costs_quota(
        self, clock: FakeClock
    ) -> None:
        limiter = make_limiter(clock)

        @limiter.limit(SHEETS, "write")
        def push_batch() -> str:
            return "sent"

        assert [push_batch(), push_batch()] == ["sent", "sent"]
        assert limiter.bucket(SHEETS, "write").used == 2

    def test_the_decorator_throttles_beyond_the_limit(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, read=60, write=2)

        @limiter.limit(SHEETS, "write")
        def push_batch() -> None:
            return None

        for _ in range(3):
            push_batch()

        assert clock.now == pytest.approx(60.0)

    def test_the_decorated_function_keeps_its_identity(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)

        @limiter.limit(SHEETS, "write")
        def push_batch() -> None:
            """Send a batch of rows."""

        assert push_batch.__name__ == "push_batch"
        assert push_batch.__doc__ == "Send a batch of rows."

    def test_a_weighted_cost_can_be_given(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter([DRIVE], clock=clock.time, sleeper=clock.sleep)

        @limiter.limit(DRIVE, "units", cost=200)
        def download() -> None:
            return None

        download()

        assert limiter.bucket(DRIVE, "units").used == 200


class TestCustomProfile:
    def test_a_profile_written_by_hand_works_the_same(self, clock: FakeClock) -> None:
        """Adding an API is data, not code -- the point of the profile shape."""
        docs = ApiProfile(
            name="docs",
            host="docs.googleapis.com",
            limits={"read": 300, "write": 60},
            resolve=resolve_sheets,
        )
        limiter = QuotaLimiter([docs], clock=clock.time, sleeper=clock.sleep)

        limiter.acquire_for("GET", "https://docs.googleapis.com/v1/documents/abc")

        assert limiter.bucket(docs, "read").used == 1


class TestPerProfileWindow:
    def test_a_profile_carries_its_own_window(self, clock: FakeClock) -> None:
        """Not every Google quota is metered per minute."""
        odd = ApiProfile(
            name="odd",
            host="odd.googleapis.com",
            limits={"queries": 1},
            resolve=resolve_sheets,
            window=100.0,
        )
        limiter = QuotaLimiter([odd], clock=clock.time, sleeper=clock.sleep)

        limiter.acquire(odd, "queries")
        assert limiter.acquire(odd, "queries") == pytest.approx(100.0)

    def test_profiles_with_different_windows_coexist(self, clock: FakeClock) -> None:
        """A limiter may hold a per-minute quota and a per-100-seconds one."""
        legacy = ApiProfile(
            name="legacy",
            host="legacy.googleapis.com",
            limits={"queries": 1},
            resolve=lambda method, path, query: ("queries", 1),
            window=100.0,
        )
        limiter = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1), legacy],
            clock=clock.time,
            sleeper=clock.sleep,
        )

        assert limiter.bucket(SHEETS, "read").window == 60.0
        assert limiter.bucket(legacy, "queries").window == 100.0

    def test_an_explicit_window_overrides_every_profile(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter(
            [SHEETS, DRIVE], window=5.0, clock=clock.time, sleeper=clock.sleep
        )

        assert limiter.bucket(SHEETS, "read").window == 5.0
        assert limiter.bucket(DRIVE, "units").window == 5.0

    def test_a_non_positive_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="window must be positive"):
            ApiProfile(
                name="bad",
                host="x",
                limits={"queries": 1},
                resolve=resolve_sheets,
                window=0,
            )


class TestObservability:
    """A limiter that works looks like a hung process. It must be visible."""

    def test_stats_start_empty_and_count_grants(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        assert limiter.stats()["sheets:read"].granted == 0

        limiter.acquire(SHEETS, "read")
        limiter.acquire(SHEETS, "read")

        stats = limiter.stats()["sheets:read"]
        assert stats.granted == 2
        assert stats.waits == 0
        assert stats.wait_seconds == 0.0

    def test_waiting_is_counted_and_timed(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, read=1, write=1)
        limiter.acquire(SHEETS, "read")
        limiter.acquire(SHEETS, "read")  # this one waits a full window

        stats = limiter.stats()["sheets:read"]
        assert stats.granted == 2
        assert stats.waits == 1
        assert stats.wait_seconds == pytest.approx(60.0)
        assert stats.average_wait == pytest.approx(60.0)

    def test_timeouts_are_counted_separately(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock, read=1, write=1)
        limiter.acquire(SHEETS, "read")

        with pytest.raises(QuotaTimeoutError):
            limiter.acquire(SHEETS, "read", timeout=1.0)

        stats = limiter.stats()["sheets:read"]
        assert stats.timeouts == 1
        assert stats.granted == 1, "a timed-out call was never granted"

    def test_average_wait_is_zero_when_nothing_waited(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        limiter.acquire(SHEETS, "read")
        assert limiter.stats()["sheets:read"].average_wait == 0.0

    def test_each_bucket_is_counted_on_its_own(self, clock: FakeClock) -> None:
        limiter = make_limiter(clock)
        limiter.acquire(SHEETS, "read")
        limiter.acquire(SHEETS, "write")
        limiter.acquire(SHEETS, "write")

        stats = limiter.stats()
        assert stats["sheets:read"].granted == 1
        assert stats["sheets:write"].granted == 2

    def test_waiting_is_logged(
        self, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Someone debugging a stalled job needs a trace, not silence."""
        limiter = make_limiter(clock, read=1, write=1)
        limiter.acquire(SHEETS, "read")

        with caplog.at_level(logging.DEBUG, logger="googleapis_without_429.core"):
            limiter.acquire(SHEETS, "read")

        messages = [record.getMessage() for record in caplog.records]
        assert any("quota exhausted" in message for message in messages)
        assert any("sheets:read" in message for message in messages)
