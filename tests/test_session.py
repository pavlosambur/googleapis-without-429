"""Tests for the rate limited session.

No network: ``requests.Session.send`` is replaced, so what is measured is the
session's own behaviour -- which bucket it charges, when it waits, when it
retries -- and nothing else.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

import pytest
import requests
from google.auth.credentials import AnonymousCredentials

from googleapis_without_429 import DRIVE, SHEETS, RateLimitedSession

from .conftest import FakeClock

SHEET_URL = "https://sheets.googleapis.com/v4/spreadsheets/abc123"
VALUES_URL = f"{SHEET_URL}/values/A1:B2"
DRIVE_URL = "https://www.googleapis.com/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret


def response(status: int = 200, **headers: str) -> requests.Response:
    made = requests.Response()
    made.status_code = status
    made.headers.update(headers)
    return made


def prepared(method: str, url: str) -> requests.PreparedRequest:
    return requests.Request(method, url).prepare()


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Replace the underlying transport and record what reaches it."""

    def install(responses: Iterable[requests.Response]) -> list[str]:
        sent: list[str] = []
        stream = iter(responses)

        def fake_send(self, request, **kwargs):
            sent.append(f"{request.method} {request.url}")
            try:
                return next(stream)
            except StopIteration:  # pragma: no cover - a test set up too few
                msg = "transport called more times than expected"
                raise AssertionError(msg) from None

        monkeypatch.setattr(requests.Session, "send", fake_send)
        return sent

    return install


def make_session(
    clock: FakeClock, *, read: int = 60, write: int = 60, **kwargs: object
) -> RateLimitedSession:
    profile = SHEETS.with_limits(read=read, write=write)
    return RateLimitedSession(
        AnonymousCredentials(),
        [profile],
        clock=clock.time,
        sleeper=clock.sleep,
        **kwargs,  # type: ignore[arg-type]
    )


class TestConstruction:
    def test_rejects_an_empty_profile_list(self, clock: FakeClock) -> None:
        with pytest.raises(ValueError, match="at least one profile"):
            RateLimitedSession(AnonymousCredentials(), [])

    def test_rejects_a_zero_attempt_budget(self, clock: FakeClock) -> None:
        with pytest.raises(ValueError, match="max_attempts must be at least 1"):
            make_session(clock, max_attempts=0)


class TestThrottling:
    def test_requests_within_the_quota_do_not_wait(
        self, clock: FakeClock, transport
    ) -> None:
        transport([response() for _ in range(3)])
        session = make_session(clock, read=3)

        for _ in range(3):
            session.send(prepared("GET", SHEET_URL))

        assert clock.slept == []

    def test_a_request_over_the_quota_waits_for_the_window(
        self, clock: FakeClock, transport
    ) -> None:
        transport([response() for _ in range(3)])
        session = make_session(clock, read=2)

        session.send(prepared("GET", SHEET_URL))
        session.send(prepared("GET", SHEET_URL))
        session.send(prepared("GET", SHEET_URL))

        assert clock.now == pytest.approx(60.0)

    def test_reads_and_writes_are_metered_separately(
        self, clock: FakeClock, transport
    ) -> None:
        """A shared counter would halve throughput for no reason."""
        transport([response() for _ in range(3)])
        session = make_session(clock, read=2, write=2)

        session.send(prepared("GET", VALUES_URL))
        session.send(prepared("GET", VALUES_URL))
        session.send(prepared("PUT", VALUES_URL))  # write bucket is untouched

        assert clock.slept == []

    def test_a_post_that_reads_is_charged_to_the_read_bucket(
        self, clock: FakeClock, transport
    ) -> None:
        transport([response() for _ in range(3)])
        session = make_session(clock, read=60, write=1)

        # Two of these would exhaust a write budget of one if misclassified.
        session.send(prepared("POST", f"{SHEET_URL}:getByDataFilter"))
        session.send(prepared("POST", f"{SHEET_URL}/values:batchGetByDataFilter"))
        session.send(prepared("POST", f"{SHEET_URL}/developerMetadata:search"))

        assert clock.slept == []


class TestOtherHosts:
    def test_requests_to_other_hosts_are_not_throttled(
        self, clock: FakeClock, transport
    ) -> None:
        """Token refreshes must not eat the quota of the API being limited."""
        transport([response() for _ in range(5)])
        session = make_session(clock, read=1, write=1)

        for _ in range(5):
            session.send(prepared("POST", TOKEN_URL))

        assert clock.slept == []

    def test_a_429_from_another_host_is_returned_untouched(
        self, clock: FakeClock, transport
    ) -> None:
        """Retrying an unknown host's 429 would be guessing at its rules."""
        sent = transport([response(429)])
        session = make_session(clock)

        result = session.send(prepared("POST", TOKEN_URL))

        assert result.status_code == 429
        assert len(sent) == 1


class TestRetryOn429:
    def test_a_429_is_retried_and_the_success_returned(
        self, clock: FakeClock, transport
    ) -> None:
        sent = transport([response(429), response(200)])
        session = make_session(clock)

        result = session.send(prepared("GET", SHEET_URL))

        assert result.status_code == 200
        assert len(sent) == 2
        assert len(clock.slept) == 1

    def test_the_last_429_is_returned_once_attempts_run_out(
        self, clock: FakeClock, transport
    ) -> None:
        """Returning it lets the caller's own client raise its own error."""
        sent = transport([response(429) for _ in range(3)])
        session = make_session(clock, max_attempts=3)

        result = session.send(prepared("GET", SHEET_URL))

        assert result.status_code == 429
        assert len(sent) == 3
        assert len(clock.slept) == 2, "no point sleeping after the final attempt"

    def test_max_attempts_of_one_disables_retrying(
        self, clock: FakeClock, transport
    ) -> None:
        sent = transport([response(429)])
        session = make_session(clock, max_attempts=1)

        assert session.send(prepared("GET", SHEET_URL)).status_code == 429
        assert len(sent) == 1
        assert clock.slept == []

    def test_every_retry_consumes_quota_again(
        self, clock: FakeClock, transport
    ) -> None:
        """A retry is a real request to Google, not a replay of the first."""
        transport([response(429), response(429), response(200)])
        session = make_session(clock, read=60)

        session.send(prepared("GET", SHEET_URL))

        bucket = session.limiter.bucket(SHEETS, "read")
        assert bucket.used == 3

    def test_a_retry_after_header_wins_over_our_own_backoff(
        self, clock: FakeClock, transport
    ) -> None:
        transport([response(429, **{"Retry-After": "17"}), response(200)])
        session = make_session(clock)

        session.send(prepared("GET", SHEET_URL))

        assert clock.slept == [17.0]

    def test_an_absurd_retry_after_is_capped(self, clock: FakeClock, transport) -> None:
        """A proxy asking for an hour should not park the process for an hour."""
        transport([response(429, **{"Retry-After": "3600"}), response(200)])
        session = make_session(clock, retry_after_cap=30.0)

        session.send(prepared("GET", SHEET_URL))

        assert clock.slept == [30.0]

    def test_an_unparseable_retry_after_falls_back_to_backoff(
        self, clock: FakeClock, transport
    ) -> None:
        transport([response(429, **{"Retry-After": "whenever"}), response(200)])
        session = make_session(clock, backoff_base=4.0)

        session.send(prepared("GET", SHEET_URL))

        assert clock.slept
        assert 2.0 <= clock.slept[0] <= 4.0


class TestProfileErrors:
    def test_a_bucket_the_profile_does_not_define_raises_clearly(
        self, clock: FakeClock, transport
    ) -> None:
        transport([response()])
        broken = SHEETS.with_limits(read=10)
        broken = type(broken)(
            name=broken.name,
            host=broken.host,
            limits={"read": 10, "write": 10},
            resolve=lambda method, path, query: ("typo", 1),
        )
        session = RateLimitedSession(
            AnonymousCredentials(),
            [broken],
            clock=clock.time,
            sleeper=clock.sleep,
        )

        with pytest.raises(ValueError, match="unknown bucket 'typo'"):
            session.send(prepared("GET", SHEET_URL))


class TestMultipleProfiles:
    def test_two_profiles_on_one_host_are_told_apart_by_path(
        self, clock: FakeClock, transport
    ) -> None:
        """Drive shares www.googleapis.com, so the path decides, not the host."""
        transport([response() for _ in range(2)])
        session = RateLimitedSession(
            AnonymousCredentials(),
            [SHEETS, DRIVE],
            clock=clock.time,
            sleeper=clock.sleep,
        )

        session.send(prepared("GET", "https://www.googleapis.com/drive/v3/files"))
        session.send(prepared("GET", "https://www.googleapis.com/calendar/v3/x"))

        assert session.limiter.bucket(DRIVE, "units").used == 100, "the Drive list"
        # The Calendar call belonged to no profile and was left alone.

    def test_duplicate_profile_names_are_rejected(self, clock: FakeClock) -> None:
        """Buckets are keyed by profile name; duplicates would silently merge."""
        with pytest.raises(ValueError, match="must be unique"):
            RateLimitedSession(AnonymousCredentials(), [SHEETS, SHEETS])

    def test_the_default_profiles_cover_sheets_and_drive(self) -> None:
        session = RateLimitedSession(AnonymousCredentials())
        assert {p.name for p in session.limiter.profiles} == {"sheets", "drive"}


class TestDriveAnswersWith403:
    """The case a 429-only retry silently missed: Drive rate-limits with 403."""

    def google_error(self, status: int, reason: str) -> requests.Response:
        made = requests.Response()
        made.status_code = status
        made._content = json.dumps(
            {
                "error": {
                    "errors": [{"domain": "usageLimits", "reason": reason}],
                    "code": status,
                    "message": reason,
                }
            }
        ).encode()
        return made

    def drive_session(self, clock: FakeClock, **kwargs: object) -> RateLimitedSession:
        return RateLimitedSession(
            AnonymousCredentials(),
            [DRIVE],
            clock=clock.time,
            sleeper=clock.sleep,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_a_rate_limit_403_is_retried(self, clock: FakeClock, transport) -> None:
        sent = transport(
            [self.google_error(403, "userRateLimitExceeded"), response(200)]
        )
        session = self.drive_session(clock)

        result = session.send(prepared("GET", DRIVE_URL))

        assert result.status_code == 200
        assert len(sent) == 2
        assert len(clock.slept) == 1

    def test_a_permission_403_is_returned_immediately(
        self, clock: FakeClock, transport
    ) -> None:
        """Retrying a refusal would turn a clear failure into a slow one."""
        sent = transport([self.google_error(403, "insufficientFilePermissions")])
        session = self.drive_session(clock)

        result = session.send(prepared("GET", DRIVE_URL))

        assert result.status_code == 403
        assert len(sent) == 1
        assert clock.slept == []

    def test_a_daily_limit_403_is_not_retried(
        self, clock: FakeClock, transport
    ) -> None:
        sent = transport([self.google_error(403, "dailyLimitExceeded")])
        session = self.drive_session(clock)

        assert session.send(prepared("GET", DRIVE_URL)).status_code == 403
        assert len(sent) == 1

    def test_every_retried_attempt_still_costs_quota(
        self, clock: FakeClock, transport
    ) -> None:
        transport(
            [
                self.google_error(403, "rateLimitExceeded"),
                self.google_error(403, "rateLimitExceeded"),
                response(200),
            ]
        )
        session = self.drive_session(clock)

        session.send(prepared("GET", DRIVE_URL))

        # Three list calls at 100 units each.
        assert session.limiter.bucket(DRIVE, "units").used == 300
