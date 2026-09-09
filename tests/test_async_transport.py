"""Tests for the asynchronous session factory.

Driven against `aiogoogle`'s real session class where it matters: the claim is
that this drops into that client, and only its own class can show that.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from aiogoogle.client import Aiogoogle
from aiogoogle.excs import HTTPError
from aiogoogle.models import Request, Response
from aiogoogle.sessions.abc import AbstractSession
from aiogoogle.sessions.aiohttp_session import AiohttpSession

from googleapis_without_429 import (
    DRIVE,
    GMAIL,
    SHEETS,
    QuotaLimiter,
    QuotaTimeoutError,
    RetryPolicy,
    rate_limited_session,
)

from .conftest import FakeClock

SHEET_URL = "https://sheets.googleapis.com/v4/spreadsheets/abc123"
GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL


class FakeSession(AbstractSession):
    """Stands in for a real client session, recording what reaches it."""

    outcomes: ClassVar[list[int | None]] = []
    sent: ClassVar[list[str]] = []

    async def send(self, *requests: Any, **kwargs: Any) -> Any:
        for request in requests:
            FakeSession.sent.append(f"{request.method} {request.url}")
        status = FakeSession.outcomes.pop(0) if FakeSession.outcomes else None
        if status is not None:
            raise HTTPError(
                f"HTTP {status}",
                res=Response(
                    status_code=status,
                    json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
                ),
            )
        return "ok"


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeSession.outcomes = []
    FakeSession.sent = []


def request(method: str, url: str) -> Request:
    return Request(method=method, url=url)


def make_session_class(clock: FakeClock, **kwargs: Any) -> type[Any]:
    limiter = kwargs.pop(
        "limiter",
        QuotaLimiter(
            [SHEETS.with_limits(read=2, write=2), DRIVE, GMAIL],
            clock=clock.time,
            sleeper=clock.sleep,
            async_sleeper=clock.async_sleep,
        ),
    )
    return rate_limited_session(
        FakeSession, limiter=limiter, sleeper=clock.async_sleep, **kwargs
    )


class TestThrottling:
    async def test_calls_within_the_quota_do_not_wait(self, clock: FakeClock) -> None:
        session = make_session_class(clock)()

        await session.send(request("GET", SHEET_URL))
        await session.send(request("GET", SHEET_URL))

        assert clock.slept == []

    async def test_a_call_over_the_quota_waits(self, clock: FakeClock) -> None:
        session = make_session_class(clock)()

        for _ in range(3):
            await session.send(request("GET", SHEET_URL))

        assert clock.now == pytest.approx(60.0)

    async def test_gmail_is_priced_per_method(self, clock: FakeClock) -> None:
        cls = make_session_class(clock)
        session = cls()

        await session.send(request("POST", GMAIL_SEND))

        assert cls.limiter.bucket(GMAIL, "units").used == 100

    async def test_an_unclaimed_host_is_not_metered(self, clock: FakeClock) -> None:
        cls = make_session_class(clock)
        session = cls()

        for _ in range(5):
            await session.send(request("POST", TOKEN_URL))

        assert clock.slept == []

    async def test_every_request_in_a_batch_is_charged(self, clock: FakeClock) -> None:
        cls = make_session_class(clock)
        session = cls()

        await session.send(request("GET", SHEET_URL), request("GET", SHEET_URL))

        assert cls.limiter.bucket(SHEETS, "read").used == 2


class TestRetrying:
    async def test_a_rate_limit_is_retried(self, clock: FakeClock) -> None:
        FakeSession.outcomes = [429]
        session = make_session_class(clock)()

        assert await session.send(request("GET", SHEET_URL)) == "ok"
        assert len(FakeSession.sent) == 2

    async def test_a_rate_limit_403_is_retried(self, clock: FakeClock) -> None:
        FakeSession.outcomes = [403]
        session = make_session_class(clock)()

        assert await session.send(request("GET", SHEET_URL)) == "ok"
        assert len(FakeSession.sent) == 2

    async def test_attempts_are_bounded(self, clock: FakeClock) -> None:
        FakeSession.outcomes = [429, 429, 429]
        session = make_session_class(clock, retry=RetryPolicy(max_attempts=3))()

        with pytest.raises(HTTPError):
            await session.send(request("GET", SHEET_URL))
        assert len(FakeSession.sent) == 3

    async def test_every_retry_costs_quota_again(self, clock: FakeClock) -> None:
        FakeSession.outcomes = [429]
        cls = make_session_class(
            clock,
            limiter=QuotaLimiter(
                [SHEETS],
                clock=clock.time,
                sleeper=clock.sleep,
                async_sleeper=clock.async_sleep,
            ),
        )
        session = cls()

        await session.send(request("GET", SHEET_URL))

        assert cls.limiter.bucket(SHEETS, "read").used == 2

    async def test_a_batch_is_never_retried(self, clock: FakeClock) -> None:
        """One error for several concurrent calls says nothing about which failed."""
        FakeSession.outcomes = [429]
        session = make_session_class(clock)()

        with pytest.raises(HTTPError):
            await session.send(request("GET", SHEET_URL), request("GET", SHEET_URL))
        assert len(FakeSession.sent) == 2, "sent once, as a pair, and not repeated"

    async def test_an_error_without_a_response_propagates(
        self, clock: FakeClock
    ) -> None:
        """A connection failure is not a rate limit; it must not be swallowed."""

        class Failing(AbstractSession):
            async def send(self, *requests: Any, **kwargs: Any) -> Any:
                raise RuntimeError("connection reset")

        cls = rate_limited_session(
            Failing,
            limiter=QuotaLimiter(
                [SHEETS],
                clock=clock.time,
                sleeper=clock.sleep,
                async_sleeper=clock.async_sleep,
            ),
            sleeper=clock.async_sleep,
        )

        with pytest.raises(RuntimeError, match="connection reset"):
            await cls().send(request("GET", SHEET_URL))

    async def test_an_error_whose_response_has_no_status_propagates(
        self, clock: FakeClock
    ) -> None:
        """Something shaped like an HTTP error but without a usable status."""

        class Odd(AbstractSession):
            async def send(self, *requests: Any, **kwargs: Any) -> Any:
                error = RuntimeError("odd")
                error.res = object()  # type: ignore[attr-defined]
                raise error

        cls = rate_limited_session(
            Odd,
            limiter=QuotaLimiter(
                [SHEETS],
                clock=clock.time,
                sleeper=clock.sleep,
                async_sleeper=clock.async_sleep,
            ),
            sleeper=clock.async_sleep,
        )

        with pytest.raises(RuntimeError, match="odd"):
            await cls().send(request("GET", SHEET_URL))

    async def test_an_unparsed_body_still_allows_a_429_retry(
        self, clock: FakeClock
    ) -> None:
        """Reasons are only needed for a 403; a 429 stands on its own."""

        class Unparsed(AbstractSession):
            calls = 0

            async def send(self, *requests: Any, **kwargs: Any) -> Any:
                Unparsed.calls += 1
                if Unparsed.calls == 1:
                    raise HTTPError(
                        "HTTP 429",
                        res=Response(status_code=429, data="<html>nope</html>"),
                    )
                return "ok"

        cls = rate_limited_session(
            Unparsed,
            limiter=QuotaLimiter(
                [SHEETS],
                clock=clock.time,
                sleeper=clock.sleep,
                async_sleeper=clock.async_sleep,
            ),
            sleeper=clock.async_sleep,
        )

        assert await cls().send(request("GET", SHEET_URL)) == "ok"
        assert Unparsed.calls == 2

    async def test_a_timeout_bounds_the_wait(self, clock: FakeClock) -> None:
        session = make_session_class(clock, acquire_timeout=5.0)()

        await session.send(request("GET", SHEET_URL))
        await session.send(request("GET", SHEET_URL))

        with pytest.raises(QuotaTimeoutError):
            await session.send(request("GET", SHEET_URL))


class TestTheFactory:
    def test_it_returns_a_subclass_of_what_it_was_given(self, clock: FakeClock) -> None:
        cls = make_session_class(clock)
        assert issubclass(cls, FakeSession)
        assert issubclass(cls, AbstractSession)

    def test_the_class_is_named_after_its_base(self, clock: FakeClock) -> None:
        assert make_session_class(clock).__name__ == "RateLimitedFakeSession"

    async def test_every_instance_shares_one_quota(self, clock: FakeClock) -> None:
        """aiogoogle builds a session per operation; each must not get a quota."""
        cls = make_session_class(clock)

        await cls().send(request("GET", SHEET_URL))
        await cls().send(request("GET", SHEET_URL))

        assert cls.limiter.bucket(SHEETS, "read").used == 2
        assert clock.slept == []

        await cls().send(request("GET", SHEET_URL))
        assert clock.now == pytest.approx(60.0), "the third instance still waited"

    async def test_it_can_share_a_limiter_with_a_sync_session(
        self, clock: FakeClock
    ) -> None:
        shared = QuotaLimiter(
            [SHEETS.with_limits(read=1, write=1)],
            clock=clock.time,
            sleeper=clock.sleep,
            async_sleeper=clock.async_sleep,
        )
        cls = rate_limited_session(
            FakeSession, limiter=shared, sleeper=clock.async_sleep
        )

        shared.acquire(SHEETS, "read")  # a synchronous caller went first
        await cls().send(request("GET", SHEET_URL))

        assert clock.now == pytest.approx(60.0), "it waited on the shared quota"


class TestAgainstTheRealClient:
    def test_aiogoogle_accepts_the_wrapped_class(self, clock: FakeClock) -> None:
        """The end of the claim: this is what goes into session_factory."""
        session_class = rate_limited_session(AiohttpSession, sleeper=clock.async_sleep)

        google = Aiogoogle(session_factory=session_class)

        assert google.session_factory is session_class
        assert issubclass(session_class, AiohttpSession)
        assert issubclass(session_class, AbstractSession)
