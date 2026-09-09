"""A paced transport for ``google-api-python-client``.

That client does not take a `requests` session. It takes an httplib2-style
object -- anything with a ``request()`` returning ``(response, content)`` --
which is why it needs an adapter of its own rather than the session.

Nothing here imports httplib2, so the dependency stays optional: the adapter
wraps whatever transport it is given.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urlparse

from googleapis_without_429.backoff import RandomSource
from googleapis_without_429.errors import reasons_in
from googleapis_without_429.limiter import QuotaLimiter
from googleapis_without_429.profiles import DRIVE, SHEETS, ApiProfile
from googleapis_without_429.retry import DEFAULT_RETRY, RetryPolicy

__all__ = ["HttpTransport", "RateLimitedHttp"]

logger = logging.getLogger(__name__)

DEFAULT_REDIRECTIONS = 5


class HttpTransport(Protocol):
    """The httplib2 shape: ``request()`` returning ``(response, content)``."""

    def request(  # noqa: PLR0913, PLR0917 - httplib2's signature, not ours
        self,
        uri: str,
        method: str = "GET",
        body: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        redirections: int = DEFAULT_REDIRECTIONS,
        connection_type: object = None,
        **kwargs: object,
    ) -> tuple[Any, bytes]:
        """Perform one HTTP request."""
        ...


class RateLimitedHttp:
    """Wraps an httplib2-style transport so it stays inside Google's quotas.

    Use it where `google-api-python-client` expects a transport::

        import google_auth_httplib2, httplib2
        from googleapiclient.discovery import build
        from googleapis_without_429 import RateLimitedHttp

        authorised = google_auth_httplib2.AuthorizedHttp(
            credentials, http=httplib2.Http()
        )
        service = build("sheets", "v4", http=RateLimitedHttp(authorised))

    Attributes not defined here are delegated to the wrapped transport, since
    the client reads things like ``timeout`` off it directly.

    Args:
        http: The transport to wrap, usually an ``AuthorizedHttp``.
        profiles: APIs to pace. Ignored when ``limiter`` is given.
        limiter: An existing limiter to share, so a session and this adapter
            can draw on one quota instead of two.
        retry: How failures are handled.
        acquire_timeout: Seconds to wait for quota before raising
            :class:`~googleapis_without_429.errors.QuotaTimeoutError`.
        rng: Randomness for the retry jitter. Injectable for testing.
        sleeper: Blocking sleep. Injectable for testing.
        clock: Monotonic time source. Injectable for testing.
    """

    def __init__(  # noqa: PLR0913 - tuning knobs, all keyword-only with defaults
        self,
        http: HttpTransport,
        profiles: Sequence[ApiProfile] = (SHEETS, DRIVE),
        *,
        limiter: QuotaLimiter | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        acquire_timeout: float | None = None,
        rng: RandomSource | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.http = http
        #: The quota buckets behind this transport. Share it with a session to
        #: keep both inside one quota.
        self.limiter = limiter or QuotaLimiter(profiles, clock=clock, sleeper=sleeper)
        self._retry = retry
        self._acquire_timeout = acquire_timeout
        self._rng = rng or random
        self._sleep = sleeper

    def __getattr__(self, name: str) -> object:
        """Delegate anything not defined here to the wrapped transport.

        `google-api-python-client` reads attributes such as ``timeout`` and
        ``connections`` straight off the transport, so an adapter that hid them
        would break in ways that are tedious to trace.
        """
        return getattr(self.http, name)

    def request(  # noqa: PLR0913, PLR0917 - httplib2's signature, not ours
        self,
        uri: str,
        method: str = "GET",
        body: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        redirections: int = DEFAULT_REDIRECTIONS,
        connection_type: object = None,
        **kwargs: object,
    ) -> tuple[Any, bytes]:
        """Pace the request against its profile, then retry what is worth it."""
        parts = urlparse(uri)
        profile = self.limiter.profile_for(parts.netloc, parts.path)
        if profile is None:
            return self.http.request(
                uri, method, body, headers, redirections, connection_type, **kwargs
            )

        bucket_name, cost = profile.resolve(method, parts.path, parts.query)
        bucket = self.limiter.bucket(profile, bucket_name)

        attempts = 0
        while True:
            # Every try consumes quota, retries included: a retry is a real
            # request to Google, not a replay of the first one.
            bucket.acquire(cost, self._acquire_timeout)
            response, content = self.http.request(
                uri, method, body, headers, redirections, connection_type, **kwargs
            )
            attempts += 1

            status = int(getattr(response, "status", 0))
            if (
                not self._retry.should_retry_status(method, status, reasons_in(content))
                or attempts >= self._retry.max_attempts
            ):
                return response, content

            # httplib2 responses are dicts with lower-cased header keys.
            retry_after = None
            if hasattr(response, "get"):
                retry_after = response.get("retry-after")
            delay = self._retry.delay_after(attempts - 1, retry_after, self._rng)
            logger.info(
                "%s %s: retrying after %d in %.2fs (attempt %d of %d)",
                method,
                parts.path,
                status,
                delay,
                attempts + 1,
                self._retry.max_attempts,
            )
            self._sleep(delay)
