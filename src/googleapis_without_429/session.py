"""The drop-in session: a Google-authorised session that paces itself."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from urllib.parse import urlparse

from google.auth.transport.requests import AuthorizedSession
from requests import PreparedRequest, Response

from googleapis_without_429.backoff import RandomSource
from googleapis_without_429.limiter import QuotaLimiter
from googleapis_without_429.profiles import DRIVE, GMAIL, SHEETS, ApiProfile
from googleapis_without_429.retry import DEFAULT_RETRY, RetryPolicy

__all__ = ["RateLimitedSession"]

logger = logging.getLogger(__name__)


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
        profiles: APIs to pace. Defaults to Sheets, Drive and Gmail. The
            first two together cover gspread, which reaches Drive to create,
            delete, share or look up a spreadsheet by title. Ignored when
            ``limiter`` is given.
        limiter: An existing limiter to share. Pass the same one to several
            sessions and they draw on a single quota, which is what threaded
            code needs: a session per thread with a limiter each would multiply
            the quota by the number of threads and hit 429 immediately.
        acquire_timeout: Seconds to wait for quota before raising
            :class:`~googleapis_without_429.errors.QuotaTimeoutError`. ``None``
            waits as long as the quota needs, which suits a batch job. Anything
            serving a request should set it: a caller that stalls for a minute
            with nothing in the log looks exactly like a hung process.
        window: Overrides every profile's own window, in seconds. Leave unset
            so each profile uses the window its API is metered over.
        retry: How failures are handled -- how many attempts, how long
            between them, and which statuses are worth repeating. See
            :class:`~googleapis_without_429.retry.RetryPolicy`.
        rng: Randomness for the jitter. Injectable for testing.
        sleeper: Blocking sleep. Injectable for testing.
        clock: Monotonic time source. Injectable for testing.
        **kwargs: Forwarded to ``AuthorizedSession``.
    """

    def __init__(  # noqa: PLR0913 - tuning knobs, all keyword-only with defaults
        self,
        credentials: object,
        profiles: Sequence[ApiProfile] = (SHEETS, DRIVE, GMAIL),
        *,
        limiter: QuotaLimiter | None = None,
        acquire_timeout: float | None = None,
        window: float | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        rng: RandomSource | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: object,
    ) -> None:
        # google-auth ships no annotations for AuthorizedSession.__init__.
        super().__init__(credentials, **kwargs)  # type: ignore[no-untyped-call]

        #: The quota buckets behind this session. Public on purpose: it is the
        #: escape hatch for pacing a call this session does not make itself,
        #: and the object to hand to another session to share a quota with.
        self.limiter = limiter or QuotaLimiter(
            profiles, window=window, clock=clock, sleeper=sleeper
        )
        self._retry = retry
        self._acquire_timeout = acquire_timeout
        self._rng = rng or random
        self._sleep = sleeper

    def send(self, request: PreparedRequest, **kwargs: object) -> Response:
        """Pace the request against its profile, then retry a 429."""
        url = urlparse(request.url or "")
        profile = self.limiter.profile_for(url.netloc, url.path)
        if profile is None:
            return super().send(request, **kwargs)  # type: ignore[arg-type]

        bucket_name, cost = profile.resolve(
            request.method or "GET", url.path, url.query
        )
        bucket = self.limiter.bucket(profile, bucket_name)

        attempts = 0
        while True:
            # Every try consumes quota, retries included -- a retry is a real
            # request to Google, not a replay of the first one.
            bucket.acquire(cost, self._acquire_timeout)
            response = super().send(request, **kwargs)  # type: ignore[arg-type]
            attempts += 1
            if (
                not self._retry.should_retry(request, response)
                or attempts >= self._retry.max_attempts
            ):
                # A spent failure is handed back rather than raised: the
                # caller's own client (gspread, say) turns it into its own
                # exception.
                return response
            delay = self._retry.delay_for(attempts - 1, response, self._rng)
            # Worth an INFO: a retried rate limit means our window and Google's
            # did not line up, which is expected occasionally and a sign the
            # limits need lowering if it happens constantly.
            logger.info(
                "%s %s: retrying after %d in %.2fs (attempt %d of %d)",
                request.method,
                url.path,
                response.status_code,
                delay,
                attempts + 1,
                self._retry.max_attempts,
            )
            self._sleep(delay)
