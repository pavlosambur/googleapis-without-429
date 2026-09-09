"""Tests for telling a rate limit apart from a refusal."""

from __future__ import annotations

import json

import pytest
import requests

from googleapis_without_429.errors import is_rate_limited, reasons_in, response_reasons


def google_error(status: int, *reasons: str) -> requests.Response:
    """A response shaped like a Google API error body."""
    made = requests.Response()
    made.status_code = status
    made._content = json.dumps(
        {
            "error": {
                "errors": [
                    {"domain": "usageLimits", "reason": reason, "message": reason}
                    for reason in reasons
                ],
                "code": status,
                "message": "; ".join(reasons),
            }
        }
    ).encode()
    made.headers["Content-Type"] = "application/json"
    return made


def raw_response(status: int, body: bytes = b"") -> requests.Response:
    made = requests.Response()
    made.status_code = status
    made._content = body
    return made


class TestRetryableLimits:
    def test_a_429_is_a_rate_limit_whatever_the_body_says(self) -> None:
        assert is_rate_limited(raw_response(429))
        assert is_rate_limited(raw_response(429, b"<html>nope</html>"))

    @pytest.mark.parametrize(
        "reason", ["rateLimitExceeded", "userRateLimitExceeded", "RATELIMITEXCEEDED"]
    )
    def test_a_403_naming_a_short_term_limit_is_retryable(self, reason: str) -> None:
        """Drive answers 403 for the condition Sheets answers 429 for."""
        assert is_rate_limited(google_error(403, reason))

    def test_one_matching_reason_among_several_is_enough(self) -> None:
        assert is_rate_limited(google_error(403, "somethingElse", "rateLimitExceeded"))


class TestLimitsThatRetryingCannotFix:
    def test_a_daily_limit_is_not_retried(self) -> None:
        """It resets at midnight Pacific; five more attempts change nothing."""
        assert not is_rate_limited(google_error(403, "dailyLimitExceeded"))

    def test_a_sharing_limit_is_not_retried(self) -> None:
        assert not is_rate_limited(google_error(403, "sharingRateLimitExceeded"))


class TestRefusalsMustNotBeRetried:
    @pytest.mark.parametrize(
        "reason",
        [
            "insufficientFilePermissions",
            "appNotAuthorizedToFile",
            "domainPolicy",
            "forbidden",
        ],
    )
    def test_a_permission_403_is_left_alone(self, reason: str) -> None:
        """Retrying a refusal turns a clear failure into a slow one."""
        assert not is_rate_limited(google_error(403, reason))

    def test_a_403_with_no_body_is_left_alone(self) -> None:
        assert not is_rate_limited(raw_response(403))

    def test_a_403_with_a_non_json_body_is_left_alone(self) -> None:
        assert not is_rate_limited(raw_response(403, b"<html>Forbidden</html>"))

    def test_other_statuses_are_not_rate_limits(self) -> None:
        for status in (200, 400, 401, 404, 500, 503):
            assert not is_rate_limited(raw_response(status))


class TestMalformedBodies:
    """A broken body must never crash the transport it was received by."""

    @pytest.mark.parametrize(
        "body",
        [
            b"[]",
            b'"just a string"',
            b"{}",
            b'{"error": "not an object"}',
            b'{"error": {"errors": "not a list"}}',
            b'{"error": {"errors": [null, 7, {"reason": 5}]}}',
            b'{"error": {"errors": [{"no_reason": "x"}]}}',
        ],
    )
    def test_unexpected_shapes_yield_no_reasons(self, body: bytes) -> None:
        assert response_reasons(raw_response(403, body)) == set()
        assert not is_rate_limited(raw_response(403, body))

    def test_reasons_are_extracted_from_a_well_formed_body(self) -> None:
        response = google_error(403, "rateLimitExceeded", "userRateLimitExceeded")
        assert response_reasons(response) == {
            "ratelimitexceeded",
            "userratelimitexceeded",
        }


class TestReasonsFromRawBodies:
    """The transport-independent parser, used by the httplib2 adapter."""

    def test_a_well_formed_body(self) -> None:
        body = json.dumps(
            {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
        ).encode()
        assert reasons_in(body) == {"ratelimitexceeded"}

    @pytest.mark.parametrize(
        "body", [None, b"", "", b"not json", "<html>", b"\xff\xfe"]
    )
    def test_unusable_bodies_yield_nothing(self, body: bytes | str | None) -> None:
        """A broken body must never crash the transport that received it."""
        assert reasons_in(body) == set()

    def test_a_string_body_works_as_well_as_bytes(self) -> None:
        body = json.dumps({"error": {"errors": [{"reason": "dailyLimitExceeded"}]}})
        assert reasons_in(body) == {"dailylimitexceeded"}
