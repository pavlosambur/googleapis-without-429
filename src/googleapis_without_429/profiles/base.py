"""The profile structure: what the limiter needs to know about one API.

A profile is data, not behaviour. Adding support for another Google API means
adding one of these, not refactoring anything -- which is also what makes a
third-party profile a reasonable pull request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

__all__ = ["READ_HTTP_METHODS", "ApiProfile", "Resolver"]

#: What a profile's ``resolve`` is called with: HTTP method, URL path, URL query.
#: The query string is needed because Drive signals a download with ``alt=media``
#: rather than with a distinct path.
Resolver = Callable[[str, str, str], "tuple[str, int]"]

READ_HTTP_METHODS = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class ApiProfile:
    """Everything the limiter needs to know about one Google API.

    Attributes:
        name: Short label, used in bucket names and error messages. Must be
            unique across the profiles given to one session.
        host: The API host. Requests to any other host are left alone, so the
            token refresh traffic to ``oauth2.googleapis.com`` never eats the
            quota of the API being limited.
        limits: Bucket name to maximum units per window. Sheets uses two
            buckets because Google meters its reads and writes separately;
            Drive uses one because Google does not.
        resolve: Maps ``(http_method, path, query)`` to ``(bucket, cost)``.
        path_prefixes: Optional path prefixes this profile claims. Needed
            because ``www.googleapis.com`` hosts more than one API, so a host
            alone no longer identifies which quota applies. Empty means the
            profile claims the whole host.
        window: Length of the quota window in seconds. Belongs to the profile
            rather than to the limiter because APIs disagree about it: Google
            meters most per minute, but some quotas are measured over 100
            seconds, and a limiter may hold both at once.
    """

    name: str
    host: str
    limits: Mapping[str, int]
    resolve: Resolver
    path_prefixes: tuple[str, ...] = ()
    window: float = 60.0

    def __post_init__(self) -> None:
        if not self.limits:
            raise ValueError(f"{self.name}: profile defines no limits")
        if self.window <= 0:
            raise ValueError(
                f"{self.name}: window must be positive, got {self.window!r}"
            )
        for bucket, limit in self.limits.items():
            if limit <= 0:
                raise ValueError(
                    f"{self.name}: limit for {bucket!r} must be positive, got {limit!r}"
                )
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    def claims(self, host: str, path: str) -> bool:
        """Whether this profile meters requests to ``host`` at ``path``."""
        if host != self.host:
            return False
        if not self.path_prefixes:
            return True
        return any(path.startswith(prefix) for prefix in self.path_prefixes)

    def with_limits(self, **overrides: int) -> ApiProfile:
        """Return a copy with some limits replaced.

        Quotas are not a property of the API alone: they depend on the project
        and Google revises them, so whatever this library ships as a default is
        eventually wrong for somebody::

            SHEETS.with_limits(read=300, write=300)
            DRIVE.with_limits(units=1_000_000)

        This changes a number, not a model. When an API meters something else
        entirely -- Drive counted *requests* before 1 May 2026 and counts
        weighted units after, over a window that need not be a minute -- build
        a profile with its own ``resolve`` and ``window`` instead. There is no
        conversion between the two, so pretending one number bridges them would
        be worse than saying so.

        Raises:
            ValueError: If a name is not a bucket of this profile. A silently
                ignored typo would leave the caller believing a limit was
                raised when it was not.
        """
        unknown = set(overrides) - set(self.limits)
        if unknown:
            known = ", ".join(sorted(self.limits))
            raise ValueError(
                f"{self.name}: unknown limit(s) {sorted(unknown)}; "
                f"this profile has: {known}"
            )
        return replace(self, limits={**self.limits, **overrides})
