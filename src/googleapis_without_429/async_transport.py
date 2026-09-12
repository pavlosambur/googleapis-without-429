"""A paced session for asynchronous Google clients, `aiogoogle` in particular.

That client takes a *class* rather than an instance -- ``Aiogoogle`` calls
``session_factory()`` itself, once per operation -- so the adapter here is a
factory that builds a subclass of whatever session class you hand it.

Nothing here imports `aiogoogle`, so it stays an optional dependency and the
same factory works for any session class whose ``send`` is a coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from googleapis_without_429.backoff import RandomSource
from googleapis_without_429.errors import reasons_in
from googleapis_without_429.limiter import QuotaLimiter
from googleapis_without_429.profiles import DRIVE, GMAIL, SHEETS, ApiProfile
from googleapis_without_429.retry import DEFAULT_RETRY, RetryPolicy

__all__ = ["rate_limited_session"]

logger = logging.getLogger(__name__)


def _error_details(error: BaseException) -> tuple[int | None, object]:
    """Status and body from a client's HTTP exception, if it carries one.

    Read by duck typing rather than by catching a specific class, which keeps
    `aiogoogle` out of this library's dependencies. Its ``HTTPError`` exposes
    ``res.status_code`` and ``res.content``; anything shaped the same works.
    """
    response = getattr(error, "res", None)
    if response is None:
        return None, None
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return None, None
    # aiogoogle parses the body before raising, so the reasons arrive as a
    # mapping under `json`; `data` holds anything it could not parse.
    body = getattr(response, "json", None)
    if body is None:
        body = getattr(response, "data", None)
    return status, body


def rate_limited_session(  # noqa: PLR0913 - tuning knobs, all keyword-only
    base: type[Any],
    *,
    limiter: QuotaLimiter | None = None,
    profiles: Sequence[ApiProfile] = (SHEETS, DRIVE, GMAIL),
    retry: RetryPolicy = DEFAULT_RETRY,
    acquire_timeout: float | None = None,
    rng: RandomSource | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> type[Any]:
    """Build a session class that paces itself, from an existing one.

    For `aiogoogle`::

        from aiogoogle import Aiogoogle
        from aiogoogle.sessions.aiohttp_session import AiohttpSession

        from googleapis_without_429 import rate_limited_session

        Session = rate_limited_session(AiohttpSession)
        async with Aiogoogle(session_factory=Session, user_creds=creds) as google:
            sheets = await google.discover("sheets", "v4")
            await google.as_user(sheets.spreadsheets.get(spreadsheetId=sheet_id))

    The quota lives in the returned class, not in its instances. That matters
    here: `aiogoogle` constructs a fresh session for each operation, so a
    limiter held per instance would hand every call its own full quota.

    Args:
        base: The session class to wrap, such as `aiogoogle`'s
            ``AiohttpSession``. Its ``send`` must be a coroutine.
        limiter: An existing limiter to share with a synchronous session, so
            one program stays inside one quota.
        profiles: APIs to pace. Ignored when ``limiter`` is given.
        retry: How failures are handled.
        acquire_timeout: Seconds to wait for quota before raising
            :class:`~googleapis_without_429.errors.QuotaTimeoutError`.
        rng: Randomness for the retry jitter. Injectable for testing.
        sleeper: Awaitable sleep used between retries. Injectable for testing.

    Returns:
        A subclass of ``base``, ready to pass as a session factory.
    """
    quota = limiter or QuotaLimiter(profiles, async_sleeper=sleeper)
    randomness = rng or random

    class RateLimitedSession(base):  # type: ignore[misc]
        """``base``, but pacing every request against a Google quota."""

        #: The shared quota buckets. Reachable so a caller can inspect usage.
        limiter = quota

        async def send(self, *requests: Any, **kwargs: Any) -> Any:
            """Pace each request, then retry a rate limit if one comes back."""
            for request in requests:
                await quota.acquire_for_async(
                    getattr(request, "method", None) or "GET",
                    getattr(request, "url", None) or "",
                    acquire_timeout,
                )

            # Retrying is only safe for a single request. `aiogoogle` sends a
            # batch concurrently and raises one error for the lot, so there is
            # no way to tell which call failed or to repeat only that one.
            if len(requests) != 1:
                return await super().send(*requests, **kwargs)

            method = getattr(requests[0], "method", None) or "GET"
            url = getattr(requests[0], "url", None) or ""
            attempts = 0
            while True:
                try:
                    return await super().send(*requests, **kwargs)
                except Exception as error:
                    status, body = _error_details(error)
                    attempts += 1
                    if status is None:
                        raise
                    reasons = reasons_in(
                        body if isinstance(body, str | bytes | Mapping) else None
                    )
                    if (
                        not retry.should_retry_status(method, status, reasons)
                        or attempts >= retry.max_attempts
                    ):
                        raise

                    delay = retry.delay_after(attempts - 1, None, randomness)
                    logger.info(
                        "%s %s: retrying after %d in %.2fs (attempt %d of %d)",
                        method,
                        url,
                        status,
                        delay,
                        attempts + 1,
                        retry.max_attempts,
                    )
                    await sleeper(delay)
                    # A retry is a real request, so it costs quota again.
                    await quota.acquire_for_async(method, url, acquire_timeout)

    RateLimitedSession.__name__ = f"RateLimited{base.__name__}"
    RateLimitedSession.__qualname__ = RateLimitedSession.__name__
    return RateLimitedSession
