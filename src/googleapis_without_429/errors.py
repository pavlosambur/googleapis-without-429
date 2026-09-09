"""Deciding whether a failed response is a rate limit worth retrying.

Google is not consistent about which status code means "you are going too
fast". Sheets answers 429. Drive answers **403** for the same condition, and
only sometimes 429, which makes a retry that watches for 429 alone silently do
nothing on Drive -- the case this library exists for.

A 403 is also the ordinary answer for "you may not touch this file", so the
status code alone cannot decide. The reason string inside the response body is
what separates them, and it also separates a limit that will clear in seconds
from one that will not clear until tomorrow.
"""

from __future__ import annotations

import json
from typing import Any

from requests import RequestException, Response

__all__ = [
    "RETRYABLE_REASONS",
    "QuotaTimeoutError",
    "is_rate_limit",
    "is_rate_limited",
    "reasons_in",
    "response_reasons",
]


class QuotaTimeoutError(TimeoutError):
    """Raised when quota did not free up within the time allowed.

    Subclasses the built-in :class:`TimeoutError`, so code that already
    handles timeouts catches this without knowing the library exists.

    Attributes:
        name: Which bucket was being waited on.
        cost: What the call was going to consume.
        waited: Seconds actually spent waiting before giving up.
        timeout: The limit that was exceeded.
    """

    def __init__(self, name: str, cost: int, waited: float, timeout: float) -> None:
        label = f"{name}: " if name else ""
        super().__init__(
            f"{label}waited {waited:.3f}s for {cost} unit(s) of quota, "
            f"giving up after {timeout:.3f}s"
        )
        self.name = name
        self.cost = cost
        self.waited = waited
        self.timeout = timeout


TOO_MANY_REQUESTS = 429
FORBIDDEN = 403

#: Reasons that mean "slow down", as opposed to "stop". Both clear on their own
#: within the minute. Compared lower-case.
#:
#: Deliberately excluded, though they are also usage limits:
#:
#: - ``dailyLimitExceeded`` resets at midnight Pacific time. Retrying for five
#:   minutes achieves nothing except five more rejected requests.
#: - ``sharingRateLimitExceeded`` is measured over a long period for the same
#:   reason.
RETRYABLE_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded"})


def reasons_in(body: bytes | str | None) -> set[str]:
    """Lower-cased ``reason`` strings from a Google API error body.

    Takes the raw body so it serves every transport: a `requests` response, an
    httplib2 tuple, anything else that ends up carrying one of these.

    Returns an empty set for a body that is missing, not JSON, or not shaped
    like a Google error. Nothing here raises: a malformed body must not turn a
    rejected request into a crash inside the transport.
    """
    if not body:
        return set()
    try:
        payload: Any = json.loads(body)
    except (ValueError, TypeError):
        return set()
    return _reasons_from_payload(payload)


def _reasons_from_payload(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    error = payload.get("error")
    if not isinstance(error, dict):
        return set()

    reasons = set()
    for item in error.get("errors") or []:
        if isinstance(item, dict):
            reason = item.get("reason")
            if isinstance(reason, str):
                reasons.add(reason.lower())
    return reasons


def response_reasons(response: Response) -> set[str]:
    """Lower-cased ``reason`` strings from a `requests` response body."""
    try:
        payload: Any = response.json()
    except (ValueError, RequestException):
        return set()
    return _reasons_from_payload(payload)


def is_rate_limit(status: int, reasons: set[str]) -> bool:
    """Whether a status and its error reasons mean "you are going too fast".

    A 429 is taken at face value. A 403 counts only when the body names a
    short-term rate limit -- otherwise it is a permission error, and retrying
    one of those turns a clear failure into a slow one.
    """
    if status == TOO_MANY_REQUESTS:
        return True
    if status != FORBIDDEN:
        return False
    return bool(reasons & RETRYABLE_REASONS)


def is_rate_limited(response: Response) -> bool:
    """Whether a `requests` response means the quota was exceeded."""
    if response.status_code == TOO_MANY_REQUESTS:
        return True
    return is_rate_limit(response.status_code, response_reasons(response))
