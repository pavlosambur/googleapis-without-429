"""Tests for the standalone limiter: the API for code that has no session."""

from __future__ import annotations

import pytest

from googleapis_without_429 import DRIVE, SHEETS, ApiProfile, QuotaLimiter
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
