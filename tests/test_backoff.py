"""Tests for retry delays and Retry-After parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from googleapis_without_429.backoff import equal_jitter_delay, parse_retry_after


class FixedRandom:
    """Returns a fixed fraction of the range, so delays are predictable."""

    def __init__(self, fraction: float) -> None:
        self.fraction = fraction

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.fraction


class TestEqualJitterDelay:
    def test_the_ceiling_doubles_with_each_attempt(self) -> None:
        highest = FixedRandom(1.0)
        delays = [equal_jitter_delay(n, base=1.0, rng=highest) for n in range(4)]
        assert delays == [1.0, 2.0, 4.0, 8.0]

    def test_half_of_the_delay_is_always_waited(self) -> None:
        """The point of equal jitter: a retry never fires immediately."""
        lowest = FixedRandom(0.0)
        delays = [equal_jitter_delay(n, base=1.0, rng=lowest) for n in range(4)]
        assert delays == [0.5, 1.0, 2.0, 4.0]

    def test_the_ceiling_stops_at_the_cap(self) -> None:
        highest = FixedRandom(1.0)
        assert equal_jitter_delay(10, base=1.0, cap=30.0, rng=highest) == 30.0

    def test_delays_stay_inside_their_bounds_with_real_randomness(self) -> None:
        for attempt in range(6):
            ceiling = min(60.0, 2**attempt)
            for _ in range(50):
                delay = equal_jitter_delay(attempt, base=1.0, cap=60.0)
                assert ceiling / 2 <= delay <= ceiling

    def test_rejects_a_negative_attempt(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            equal_jitter_delay(-1)


class TestParseRetryAfter:
    def test_absent_header_means_no_instruction(self) -> None:
        assert parse_retry_after(None) is None

    def test_blank_header_means_no_instruction(self) -> None:
        assert parse_retry_after("   ") is None

    def test_seconds_form(self) -> None:
        assert parse_retry_after("30") == 30.0

    def test_negative_seconds_are_clamped_to_zero(self) -> None:
        assert parse_retry_after("-5") == 0.0

    def test_http_date_form(self) -> None:
        now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
        when = now + timedelta(seconds=45)
        header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")

        assert parse_retry_after(header, now=now) == pytest.approx(45.0)

    def test_a_date_in_the_past_is_clamped_to_zero(self) -> None:
        now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
        header = (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")

        assert parse_retry_after(header, now=now) == 0.0

    def test_unparseable_header_falls_back_to_our_own_backoff(self) -> None:
        """None means 'no instruction', not 'retry now'."""
        assert parse_retry_after("soon please") is None


def test_an_http_date_without_a_timezone_is_read_as_utc() -> None:
    """RFC 9110 dates are always GMT, but a sloppy server may omit the suffix."""
    now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    assert parse_retry_after("Tue, 08 Sep 2026 12:00:20", now=now) == pytest.approx(
        20.0
    )
