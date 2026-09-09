"""Tests for the retry policy.

The interesting question is not "does it retry" but "what does it refuse to
retry": a duplicated write is worse than a visible failure.
"""

from __future__ import annotations

import json

import pytest
import requests

from googleapis_without_429 import DEFAULT_RETRY, RetryPolicy


def response(status: int, **headers: str) -> requests.Response:
    made = requests.Response()
    made.status_code = status
    made.headers.update(headers)
    return made


def rate_limit_403() -> requests.Response:
    made = requests.Response()
    made.status_code = 403
    made._content = json.dumps(
        {"error": {"errors": [{"reason": "userRateLimitExceeded"}], "code": 403}}
    ).encode()
    return made


def request(method: str) -> requests.PreparedRequest:
    url = "https://sheets.googleapis.com/v4/spreadsheets/abc/values/A1:B2:append"
    return requests.Request(method, url).prepare()


class TestValidation:
    def test_rejects_a_zero_attempt_budget(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be at least 1"):
            RetryPolicy(max_attempts=0)

    def test_rejects_a_non_positive_base(self) -> None:
        with pytest.raises(ValueError, match="backoff_base must be positive"):
            RetryPolicy(backoff_base=0)

    def test_rejects_a_cap_below_the_base(self) -> None:
        """Otherwise every delay is silently pinned to the floor."""
        with pytest.raises(ValueError, match="is below backoff_base"):
            RetryPolicy(backoff_base=10.0, backoff_cap=1.0)

    def test_rejects_a_negative_retry_after_cap(self) -> None:
        with pytest.raises(ValueError, match="retry_after_cap must not be negative"):
            RetryPolicy(retry_after_cap=-1.0)


class TestRateLimits:
    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
    def test_a_429_is_retried_for_any_method(self, method: str) -> None:
        """A rate limit means rejected, not half-applied, so repeating is safe."""
        assert DEFAULT_RETRY.should_retry(request(method), response(429))

    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_a_rate_limit_403_is_retried_for_any_method(self, method: str) -> None:
        assert DEFAULT_RETRY.should_retry(request(method), rate_limit_403())


class TestServerErrors:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_server_error_is_retried_for_a_read(self, status: int) -> None:
        assert DEFAULT_RETRY.should_retry(request("GET"), response(status))

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_server_error_is_not_retried_for_a_post(self, status: int) -> None:
        """The server may have appended the row before failing to say so."""
        assert not DEFAULT_RETRY.should_retry(request("POST"), response(status))

    @pytest.mark.parametrize("method", ["PUT", "DELETE", "HEAD", "OPTIONS"])
    def test_idempotent_methods_are_retried(self, method: str) -> None:
        assert DEFAULT_RETRY.should_retry(request(method), response(503))

    def test_unsafe_retries_can_be_opted_into(self) -> None:
        policy = RetryPolicy(retry_unsafe_server_errors=True)
        assert policy.should_retry(request("POST"), response(503))

    def test_server_error_retries_can_be_switched_off(self) -> None:
        policy = RetryPolicy(retry_server_errors=False)
        assert not policy.should_retry(request("GET"), response(503))
        assert policy.should_retry(request("GET"), response(429)), "429 still retried"

    def test_the_status_set_is_configurable(self) -> None:
        policy = RetryPolicy(server_error_statuses=frozenset({503}))
        assert policy.should_retry(request("GET"), response(503))
        assert not policy.should_retry(request("GET"), response(500))


class TestNotRetried:
    @pytest.mark.parametrize("status", [200, 201, 400, 401, 404, 409, 422])
    def test_ordinary_responses_are_left_alone(self, status: int) -> None:
        assert not DEFAULT_RETRY.should_retry(request("GET"), response(status))

    def test_a_permission_403_is_left_alone(self) -> None:
        made = requests.Response()
        made.status_code = 403
        made._content = json.dumps(
            {"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}
        ).encode()
        assert not DEFAULT_RETRY.should_retry(request("GET"), made)

    def test_a_request_without_a_method_is_treated_as_a_get(self) -> None:
        made = requests.PreparedRequest()
        made.method = None
        assert DEFAULT_RETRY.should_retry(made, response(503))


class TestDelays:
    def test_retry_after_wins_over_computed_backoff(self) -> None:
        delay = DEFAULT_RETRY.delay_for(0, response(429, **{"Retry-After": "17"}))
        assert delay == 17.0

    def test_retry_after_is_capped(self) -> None:
        policy = RetryPolicy(retry_after_cap=30.0)
        delay = policy.delay_for(0, response(429, **{"Retry-After": "3600"}))
        assert delay == 30.0

    def test_without_a_header_the_delay_grows_and_stays_bounded(self) -> None:
        policy = RetryPolicy(backoff_base=1.0, backoff_cap=8.0)
        for attempt in range(6):
            ceiling = min(8.0, 2.0**attempt)
            delay = policy.delay_for(attempt, response(429))
            assert ceiling / 2 <= delay <= ceiling
