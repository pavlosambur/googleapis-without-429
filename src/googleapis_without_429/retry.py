"""When to try a failed request again, and how long to wait first.

Kept apart from the session because retrying is a policy, not a mechanism:
what counts as worth retrying differs between a batch job that can afford to
be slow and a request handler that cannot, and between a read that can safely
be repeated and a write that cannot.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from requests import PreparedRequest, Response

from googleapis_without_429.backoff import (
    RandomSource,
    equal_jitter_delay,
    parse_retry_after,
)
from googleapis_without_429.errors import is_rate_limited

__all__ = ["DEFAULT_RETRY", "RetryPolicy"]

#: Methods that may be repeated without changing the result beyond the first
#: attempt (RFC 9110). A 5xx means the server may have applied the change
#: before failing to answer, so repeating anything else can duplicate it --
#: `values:append` is exactly that shape, and a duplicated row is worse than
#: an error.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: Server-side failures Google's own guidance says to retry with backoff.
SERVER_ERROR_STATUSES = frozenset({500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """How a session responds to a failed request.

    Attributes:
        max_attempts: Total tries per request, including the first.
        backoff_base: Ceiling for the first retry delay, in seconds.
        backoff_cap: Upper bound on the delay, so it stops doubling.
        retry_after_cap: Longest ``Retry-After`` that will be honoured. A
            server or proxy asking for an hour would otherwise park the
            process for an hour with nothing to show for it.
        retry_server_errors: Whether 5xx responses are retried at all.
        server_error_statuses: Which statuses count as a server error.
        idempotent_methods: Methods safe to repeat after a 5xx. A server error
            leaves it unknown whether the request took effect, so repeating a
            POST can duplicate it.
        retry_unsafe_server_errors: Retry 5xx even for methods outside
            ``idempotent_methods``. Off by default, because a duplicated write
            is a worse outcome than a failed one. Turn it on only where the
            calls are known to be safe to repeat.
    """

    max_attempts: int = 5
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    retry_after_cap: float = 300.0
    retry_server_errors: bool = True
    server_error_statuses: frozenset[int] = field(default=SERVER_ERROR_STATUSES)
    idempotent_methods: frozenset[str] = field(default=IDEMPOTENT_METHODS)
    retry_unsafe_server_errors: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be at least 1, got {self.max_attempts!r}"
            )
        if self.backoff_base <= 0:
            raise ValueError(
                f"backoff_base must be positive, got {self.backoff_base!r}"
            )
        if self.backoff_cap < self.backoff_base:
            raise ValueError(
                f"backoff_cap ({self.backoff_cap!r}) is below backoff_base "
                f"({self.backoff_base!r}), which would cap every delay at the floor"
            )
        if self.retry_after_cap < 0:
            raise ValueError(
                f"retry_after_cap must not be negative, got {self.retry_after_cap!r}"
            )

    def should_retry(self, request: PreparedRequest, response: Response) -> bool:
        """Whether this response is worth trying again.

        Rate limits always are: they clear on their own. Server errors are
        only when the request can be repeated safely, since a 5xx leaves it
        unknown whether the call already took effect.
        """
        if is_rate_limited(response):
            return True
        if not self.retry_server_errors:
            return False
        if response.status_code not in self.server_error_statuses:
            return False
        if self.retry_unsafe_server_errors:
            return True
        method = (request.method or "GET").upper()
        return method in self.idempotent_methods

    def delay_for(
        self, attempt: int, response: Response, rng: RandomSource | None = None
    ) -> float:
        """Seconds to wait before retry number ``attempt`` (zero-based).

        The server's own instruction wins when it sends one, bounded so a
        misconfigured proxy cannot stall the process indefinitely.
        """
        hinted = parse_retry_after(response.headers.get("Retry-After"))
        if hinted is not None:
            return min(hinted, self.retry_after_cap)
        return equal_jitter_delay(
            attempt, self.backoff_base, self.backoff_cap, rng or random
        )


#: The policy used when none is given.
DEFAULT_RETRY = RetryPolicy()
