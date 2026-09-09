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

from typing import Any

from requests import RequestException, Response

__all__ = ["RETRYABLE_REASONS", "is_rate_limited", "response_reasons"]

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


def response_reasons(response: Response) -> set[str]:
    """Lower-cased ``reason`` strings from a Google API error body.

    Returns an empty set for a body that is missing, not JSON, or not shaped
    like a Google error. Nothing here raises: a malformed body must not turn a
    rejected request into a crash inside the transport.
    """
    try:
        payload: Any = response.json()
    except (ValueError, RequestException):
        return set()

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


def is_rate_limited(response: Response) -> bool:
    """Whether this response means the quota was exceeded and retrying may help.

    A 429 is taken at face value. A 403 counts only when the body names a
    short-term rate limit -- otherwise it is a permission error, and retrying
    one of those turns a clear failure into a slow one.
    """
    if response.status_code == TOO_MANY_REQUESTS:
        return True
    if response.status_code != FORBIDDEN:
        return False
    return bool(response_reasons(response) & RETRYABLE_REASONS)
