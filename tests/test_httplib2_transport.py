"""Tests for the google-api-python-client adapter.

Driven through a real `googleapiclient` service where possible: the claim is
that this drops into that client, and only that client can demonstrate it.
"""

from __future__ import annotations

import json
from typing import Any

import httplib2
import pytest
from googleapiclient.discovery import build

from googleapis_without_429 import (
    DRIVE,
    SHEETS,
    QuotaLimiter,
    QuotaTimeoutError,
    RateLimitedHttp,
    RetryPolicy,
)

from .conftest import FakeClock

SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
SHEETS_URL = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
DRIVE_URL = "https://www.googleapis.com/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL


class FakeHttp:
    """A stand-in transport that replays prepared responses."""

    def __init__(self, replies: list[tuple[int, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.timeout = 42  # an attribute the real client reads off the transport

    def request(  # noqa: PLR0913, PLR0917 - matching httplib2
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        redirections: int = 5,
        connection_type: Any = None,
        **kwargs: Any,
    ) -> tuple[httplib2.Response, bytes]:
        self.calls.append((method, uri))
        status, payload = self.replies.pop(0)
        response = httplib2.Response({"status": status})
        if isinstance(payload, dict) and "headers" in payload:
            response.update(payload["headers"])
            payload = payload["body"]
        content = json.dumps(payload).encode() if payload is not None else b""
        return response, content


def error_body(reason: str) -> dict[str, Any]:
    return {"error": {"errors": [{"reason": reason}], "code": 403}}


def make_http(clock: FakeClock, replies: list[tuple[int, Any]], **kwargs: Any):
    fake = FakeHttp(replies)
    limited = RateLimitedHttp(
        fake,
        [SHEETS.with_limits(read=2, write=2), DRIVE],
        clock=clock.time,
        sleeper=clock.sleep,
        **kwargs,
    )
    return fake, limited


class TestThrottling:
    def test_calls_within_the_quota_do_not_wait(self, clock: FakeClock) -> None:
        _, http = make_http(clock, [(200, {})] * 2)

        http.request(SHEETS_URL)
        http.request(SHEETS_URL)

        assert clock.slept == []

    def test_a_call_over_the_quota_waits(self, clock: FakeClock) -> None:
        _, http = make_http(clock, [(200, {})] * 3)

        for _ in range(3):
            http.request(SHEETS_URL)

        assert clock.now == pytest.approx(60.0)

    def test_reads_and_writes_use_separate_buckets(self, clock: FakeClock) -> None:
        _, http = make_http(clock, [(200, {})] * 3)

        http.request(SHEETS_URL)
        http.request(SHEETS_URL)
        http.request(SHEETS_URL, method="POST")

        assert clock.slept == []

    def test_drive_is_priced_in_units(self, clock: FakeClock) -> None:
        _, http = make_http(clock, [(200, {})])

        http.request(DRIVE_URL)

        assert http.limiter.bucket(DRIVE, "units").used == 100

    def test_an_unclaimed_host_passes_straight_through(self, clock: FakeClock) -> None:
        fake, http = make_http(clock, [(200, {})] * 5)

        for _ in range(5):
            http.request(TOKEN_URL, method="POST")

        assert clock.slept == []
        assert len(fake.calls) == 5


class TestRetrying:
    def test_a_429_is_retried(self, clock: FakeClock) -> None:
        fake, http = make_http(clock, [(429, None), (200, {})])

        response, _ = http.request(SHEETS_URL)

        assert response.status == 200
        assert len(fake.calls) == 2

    def test_a_rate_limit_403_is_retried(self, clock: FakeClock) -> None:
        """Drive's shape: a 403 whose body names a short-term limit."""
        fake, http = make_http(
            clock, [(403, error_body("userRateLimitExceeded")), (200, {})]
        )

        response, _ = http.request(DRIVE_URL)

        assert response.status == 200
        assert len(fake.calls) == 2

    def test_a_permission_403_is_returned_immediately(self, clock: FakeClock) -> None:
        fake, http = make_http(
            clock, [(403, error_body("insufficientFilePermissions"))]
        )

        response, _ = http.request(DRIVE_URL)

        assert response.status == 403
        assert len(fake.calls) == 1

    def test_a_503_on_a_read_is_retried(self, clock: FakeClock) -> None:
        fake, http = make_http(clock, [(503, None), (200, {})])

        http.request(SHEETS_URL)

        assert len(fake.calls) == 2

    def test_a_503_on_a_post_is_not_retried(self, clock: FakeClock) -> None:
        """The same duplicate-write reasoning as the session."""
        fake, http = make_http(clock, [(503, None)])

        response, _ = http.request(f"{SHEETS_URL}/values/A1:B2:append", method="POST")

        assert response.status == 503
        assert len(fake.calls) == 1

    def test_retry_after_is_honoured(self, clock: FakeClock) -> None:
        _, http = make_http(
            clock,
            [(429, {"headers": {"retry-after": "13"}, "body": None}), (200, {})],
        )

        http.request(SHEETS_URL)

        assert clock.slept == [13.0]

    def test_attempts_are_bounded(self, clock: FakeClock) -> None:
        fake, http = make_http(
            clock, [(429, None)] * 3, retry=RetryPolicy(max_attempts=3)
        )

        response, _ = http.request(SHEETS_URL)

        assert response.status == 429
        assert len(fake.calls) == 3

    def test_every_attempt_costs_quota(self, clock: FakeClock) -> None:
        """A retry is a real request to Google, not a replay of the first."""
        http = RateLimitedHttp(
            FakeHttp([(429, None), (429, None), (200, {})]),
            [SHEETS],  # the full quota, so the window never rolls mid-test
            clock=clock.time,
            sleeper=clock.sleep,
        )

        http.request(SHEETS_URL)

        assert http.limiter.bucket(SHEETS, "read").used == 3


class TestAdapterBehaviour:
    def test_unknown_attributes_reach_the_wrapped_transport(
        self, clock: FakeClock
    ) -> None:
        """The client reads timeout and friends straight off the transport."""
        _, http = make_http(clock, [])
        assert http.timeout == 42

    def test_a_timeout_can_bound_the_wait(self, clock: FakeClock) -> None:
        _, http = make_http(clock, [(200, {})] * 3, acquire_timeout=5.0)

        http.request(SHEETS_URL)
        http.request(SHEETS_URL)

        with pytest.raises(QuotaTimeoutError):
            http.request(SHEETS_URL)

    def test_it_can_share_a_limiter_with_a_session(self, clock: FakeClock) -> None:
        shared = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1)],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        http = RateLimitedHttp(
            FakeHttp([(200, {})] * 2),
            limiter=shared,
            clock=clock.time,
            sleeper=clock.sleep,
        )

        http.request(SHEETS_URL)
        http.request(SHEETS_URL)

        assert clock.now == pytest.approx(60.0)
        assert shared.bucket(SHEETS, "read").used == 1


class TestThroughGoogleApiPythonClient:
    """The claim under test: this drops into the official client unchanged.

    `static_discovery=True` builds the service from the description shipped
    inside googleapiclient, so no network is involved here either.
    """

    def build_sheets(self, clock: FakeClock, replies: list[tuple[int, Any]]):
        fake = FakeHttp(replies)
        http = RateLimitedHttp(
            fake,
            [SHEETS.with_limits(read=2, write=2), DRIVE],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        service = build("sheets", "v4", http=http, static_discovery=True)
        return fake, http, service

    def test_a_read_goes_through_the_read_quota(self, clock: FakeClock) -> None:
        _, http, service = self.build_sheets(
            clock, [(200, {"spreadsheetId": SHEET_ID})]
        )

        result = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()

        assert result["spreadsheetId"] == SHEET_ID
        assert http.limiter.bucket(SHEETS, "read").used == 1
        assert http.limiter.bucket(SHEETS, "write").used == 0

    def test_a_write_goes_through_the_write_quota(self, clock: FakeClock) -> None:
        _, http, service = self.build_sheets(clock, [(200, {"updatedCells": 2})])

        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=SHEET_ID,
                range="A1:B2",
                valueInputOption="RAW",
                body={"values": [["a", "b"]]},
            )
            .execute()
        )

        assert http.limiter.bucket(SHEETS, "write").used == 1
        assert http.limiter.bucket(SHEETS, "read").used == 0

    def test_a_batch_get_is_a_read_despite_its_shape(self, clock: FakeClock) -> None:
        _, http, service = self.build_sheets(clock, [(200, {"valueRanges": []})])

        (
            service.spreadsheets()
            .values()
            .batchGet(spreadsheetId=SHEET_ID, ranges=["A1:B2"])
            .execute()
        )

        assert http.limiter.bucket(SHEETS, "read").used == 1

    def test_a_loop_of_reads_throttles_with_no_change_to_the_caller(
        self, clock: FakeClock
    ) -> None:
        _, _http, service = self.build_sheets(
            clock, [(200, {"spreadsheetId": SHEET_ID})] * 3
        )

        for _ in range(3):
            service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()

        assert clock.now == pytest.approx(60.0), "the third read waited its turn"

    def test_a_rate_limited_call_is_retried_under_the_client(
        self, clock: FakeClock
    ) -> None:
        fake, _http, service = self.build_sheets(
            clock, [(429, None), (200, {"spreadsheetId": SHEET_ID})]
        )

        result = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()

        assert result["spreadsheetId"] == SHEET_ID
        assert len(fake.calls) == 2


class TestUnusualResponses:
    def test_a_response_without_header_access_still_retries(
        self, clock: FakeClock
    ) -> None:
        """Not every transport returns a dict-like response object."""

        class BareResponse:
            def __init__(self, status: int) -> None:
                self.status = status

        class BareHttp:
            def __init__(self) -> None:
                self.replies = [BareResponse(429), BareResponse(200)]
                self.calls = 0

            def request(self, uri: str, method: str = "GET", *args: Any, **kw: Any):
                self.calls += 1
                return self.replies.pop(0), b""

        bare = BareHttp()
        http = RateLimitedHttp(bare, [SHEETS], clock=clock.time, sleeper=clock.sleep)

        response, _ = http.request(SHEETS_URL)

        assert response.status == 200
        assert bare.calls == 2
        assert clock.slept, "it fell back on computed backoff"
