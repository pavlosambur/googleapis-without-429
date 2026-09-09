"""The drop-in session: a Google-authorised session that paces itself."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from urllib.parse import urlparse

from google.auth.transport.requests import AuthorizedSession
from requests import PreparedRequest, Response

from googleapis_without_429.backoff import equal_jitter_delay, parse_retry_after
from googleapis_without_429.core import WeightedSlidingWindow
from googleapis_without_429.profiles import DRIVE, SHEETS, ApiProfile

__all__ = ["RateLimitedSession"]

TOO_MANY_REQUESTS = 429


class RateLimitedSession(AuthorizedSession):
    """An ``AuthorizedSession`` that waits rather than exceeding a quota.

    Pass one to any client that accepts a session -- gspread does -- and the
    rest of the calling code is unchanged::

        session = RateLimitedSession(credentials)
        client = gspread.authorize(credentials, session=session)

    Requests that no profile claims pass straight through. That is not a
    detail: an authorised session fetches tokens from ``oauth2.googleapis.com``,
    and those calls must not consume the quota of the API being limited.

    Args:
        credentials: Google credentials, as for ``AuthorizedSession``.
        profiles: APIs to pace. Defaults to Sheets and Drive, which together
            cover gspread -- it reaches Drive to create, delete, share or
            look up a spreadsheet by title.
        window: Length of the quota window in seconds. Google meters per minute.
        max_attempts: Total tries per request, including the first. The retry
            exists because our window and Google's are not aligned; see
            :mod:`googleapis_without_429.backoff`.
        backoff_base: Ceiling for the first retry delay, in seconds.
        backoff_cap: Upper bound on the retry delay, in seconds.
        retry_after_cap: Longest ``Retry-After`` that will be honoured. A
            server or proxy asking for an hour would otherwise park the process
            for an hour with nothing to show for it.
        rng: Randomness for the jitter. Injectable for testing.
        sleeper: Blocking sleep. Injectable for testing.
        clock: Monotonic time source. Injectable for testing.
        **kwargs: Forwarded to ``AuthorizedSession``.
    """

    def __init__(  # noqa: PLR0913 - tuning knobs, all keyword-only with defaults
        self,
        credentials: object,
        profiles: Sequence[ApiProfile] = (SHEETS, DRIVE),
        *,
        window: float = 60.0,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        retry_after_cap: float = 300.0,
        rng: random.Random | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: object,
    ) -> None:
        # google-auth ships no annotations for AuthorizedSession.__init__.
        super().__init__(credentials, **kwargs)  # type: ignore[no-untyped-call]

        if not profiles:
            raise ValueError("at least one profile is required")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts!r}")

        names = [profile.name for profile in profiles]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(
                f"profile names must be unique, got duplicates: {sorted(duplicates)}"
            )

        # A list rather than a dict keyed by host: www.googleapis.com serves
        # more than one API, so the host alone no longer picks a profile.
        self._profiles = tuple(profiles)
        self._buckets = {
            (profile.name, bucket): WeightedSlidingWindow(
                limit,
                window,
                name=f"{profile.name}:{bucket}",
                clock=clock,
                sleeper=sleeper,
            )
            for profile in profiles
            for bucket, limit in profile.limits.items()
        }
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._retry_after_cap = retry_after_cap
        self._rng = rng or random
        self._sleep = sleeper

    def send(self, request: PreparedRequest, **kwargs: object) -> Response:
        """Pace the request against its profile, then retry a 429."""
        url = urlparse(request.url or "")
        profile = self._profile_for(url.netloc, url.path)
        if profile is None:
            return super().send(request, **kwargs)  # type: ignore[arg-type]

        bucket_name, cost = profile.resolve(
            request.method or "GET", url.path, url.query
        )
        try:
            bucket = self._buckets[(profile.name, bucket_name)]
        except KeyError:
            known = ", ".join(sorted(profile.limits))
            raise ValueError(
                f"{profile.name}: resolve() returned unknown bucket "
                f"{bucket_name!r} for {request.method} {url.path}; "
                f"this profile defines: {known}"
            ) from None

        attempts = 0
        while True:
            # Every try consumes quota, retries included -- a retry is a real
            # request to Google, not a replay of the first one.
            bucket.acquire(cost)
            response = super().send(request, **kwargs)  # type: ignore[arg-type]
            attempts += 1
            if (
                response.status_code != TOO_MANY_REQUESTS
                or attempts >= self._max_attempts
            ):
                # A spent 429 is handed back rather than raised: the caller's
                # own client (gspread, say) turns it into its own exception.
                return response
            self._sleep(self._delay_after_429(attempts - 1, response))

    def _profile_for(self, host: str, path: str) -> ApiProfile | None:
        """The first profile claiming this host and path, if any."""
        for profile in self._profiles:
            if profile.claims(host, path):
                return profile
        return None

    def _delay_after_429(self, attempt: int, response: Response) -> float:
        """Prefer the server's instruction, fall back on our own backoff."""
        hinted = parse_retry_after(response.headers.get("Retry-After"))
        if hinted is not None:
            return min(hinted, self._retry_after_cap)
        return equal_jitter_delay(
            attempt, self._backoff_base, self._backoff_cap, self._rng
        )
