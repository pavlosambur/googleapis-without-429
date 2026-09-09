"""Quota buckets for a set of profiles, usable without a session.

This is the layer between the raw sliding window and the drop-in session. Reach
for it when the calls are made by something other than a `requests` session --
a hand-rolled client, a background worker, an API this library has no adapter
for yet.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from urllib.parse import urlparse

from googleapis_without_429.core import WeightedSlidingWindow
from googleapis_without_429.profiles import DRIVE, SHEETS, ApiProfile

__all__ = ["QuotaLimiter"]


class QuotaLimiter:
    """Holds one sliding window per bucket of each profile it is given.

    Args:
        profiles: APIs to meter. Defaults to Sheets and Drive.
        window: Length of the quota window in seconds. Google meters per minute.
        clock: Monotonic time source. Injectable for testing.
        sleeper: Blocking sleep. Injectable for testing.

    Raises:
        ValueError: If no profiles are given, or two share a name. Buckets are
            keyed by profile name, so duplicates would silently share a quota.
    """

    def __init__(
        self,
        profiles: Sequence[ApiProfile] = (SHEETS, DRIVE),
        *,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not profiles:
            raise ValueError("at least one profile is required")

        names = [profile.name for profile in profiles]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(
                f"profile names must be unique, got duplicates: {sorted(duplicates)}"
            )

        # A sequence rather than a mapping by host: www.googleapis.com serves
        # more than one API, so a host alone no longer picks a profile.
        self.profiles = tuple(profiles)
        self._buckets = {
            (profile.name, name): WeightedSlidingWindow(
                limit,
                window,
                name=f"{profile.name}:{name}",
                clock=clock,
                sleeper=sleeper,
            )
            for profile in profiles
            for name, limit in profile.limits.items()
        }

    def __repr__(self) -> str:
        names = ", ".join(profile.name for profile in self.profiles)
        return f"<{type(self).__name__} [{names}]>"

    def profile_for(self, host: str, path: str) -> ApiProfile | None:
        """The first profile claiming this host and path, if any."""
        for profile in self.profiles:
            if profile.claims(host, path):
                return profile
        return None

    def bucket(self, profile: ApiProfile, name: str) -> WeightedSlidingWindow:
        """The sliding window backing one bucket of one profile.

        The lowest-level handle this library offers. Useful for inspecting
        current usage, or for pacing something this library does not model.

        Raises:
            ValueError: If the profile is not metered here, or has no such
                bucket.
        """
        try:
            return self._buckets[(profile.name, name)]
        except KeyError:
            if not any(known.name == profile.name for known in self.profiles):
                metered = ", ".join(known.name for known in self.profiles)
                raise ValueError(
                    f"profile {profile.name!r} is not metered by this limiter; "
                    f"it holds: {metered}"
                ) from None
            known_buckets = ", ".join(sorted(profile.limits))
            raise ValueError(
                f"{profile.name}: unknown bucket {name!r}; "
                f"this profile defines: {known_buckets}"
            ) from None

    def acquire(self, profile: ApiProfile, bucket: str, cost: int = 1) -> float:
        """Block until ``cost`` fits in that bucket, then consume it.

        Returns:
            Seconds spent waiting. ``0.0`` means the call passed straight
            through, which is the common case below the quota.
        """
        return self.bucket(profile, bucket).acquire(cost)

    def acquire_for(self, http_method: str, url: str) -> float:
        """Pace one request by its URL, letting the profile classify it.

        Returns ``0.0`` for a URL no profile claims, so calls to unrelated
        hosts are never charged to somebody else's quota.
        """
        parts = urlparse(url)
        profile = self.profile_for(parts.netloc, parts.path)
        if profile is None:
            return 0.0
        bucket, cost = profile.resolve(http_method, parts.path, parts.query)
        return self.acquire(profile, bucket, cost)

    @contextmanager
    def limit(self, profile: ApiProfile, bucket: str, cost: int = 1) -> Iterator[float]:
        """Wait for quota, then run the block.

        Works as a context manager and, because of how `contextlib` builds it,
        as a decorator too::

            with limiter.limit(SHEETS, "write"):
                requests.post(url, json=payload)

            @limiter.limit(SHEETS, "write")
            def push_batch(rows): ...

        Yields:
            Seconds spent waiting, for the context-manager form.
        """
        yield self.acquire(profile, bucket, cost)
