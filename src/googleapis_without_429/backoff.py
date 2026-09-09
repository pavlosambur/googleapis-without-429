"""Retry delays: exponential backoff with jitter, and ``Retry-After`` parsing.

The limiter is the first line of defence and the retry is the second. Both are
needed: our window slides over our own timestamps, while Google meters fixed
windows whose boundaries we cannot see, so a burst that looks safe to us can
still straddle two of Google's windows and come back 429.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

__all__ = ["RandomSource", "equal_jitter_delay", "parse_retry_after"]


class RandomSource(Protocol):
    """Anything offering ``uniform``: the :mod:`random` module, or a Random."""

    def uniform(self, a: float, b: float) -> float:
        """Return a float between ``a`` and ``b``."""
        ...


def equal_jitter_delay(
    attempt: int,
    base: float = 1.0,
    cap: float = 60.0,
    rng: RandomSource = random,
) -> float:
    """Return the delay before retry number ``attempt`` (zero-based).

    Half of the exponential ceiling is always waited, and the other half is
    randomised. The randomness matters when several threads are retrying at
    once: without it they wake together and collide again.

    The guaranteed half matters too, which is why this is not the more common
    "full jitter" (a uniform draw from zero to the ceiling). A 429 means the
    quota window has not reopened yet, so a delay that comes out near zero
    buys nothing but another 429.

    Args:
        attempt: Zero-based retry number. Each one doubles the ceiling.
        base: Ceiling for the first retry, in seconds.
        cap: Upper bound on the ceiling, so it stops doubling eventually.
        rng: Source of randomness. Injectable for testing.
    """
    if attempt < 0:
        raise ValueError(f"attempt must not be negative, got {attempt!r}")
    # 2.0 rather than 2: `int ** int` is Any to a type checker, because a
    # negative exponent would produce a float.
    ceiling = min(cap, base * (2.0**attempt))
    half = ceiling / 2
    return half + rng.uniform(0.0, half)


def parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    """Convert a ``Retry-After`` header into seconds from now.

    The header comes in two shapes (RFC 9110): a number of seconds, or an HTTP
    date. The date form must be compared against wall-clock time rather than a
    monotonic clock, since it is an absolute instant.

    Returns:
        Seconds to wait, never negative, or ``None`` if the header is absent or
        unparseable. ``None`` means "no server instruction", so the caller
        falls back on its own backoff rather than retrying immediately.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    try:
        return max(float(text), 0.0)
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    reference = now or datetime.now(timezone.utc)
    return max((when - reference).total_seconds(), 0.0)
