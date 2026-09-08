"""API profiles: which host, which quota buckets, and what a call costs.

A profile is data, not behaviour. Adding support for another Google API means
adding one of these, not refactoring anything -- which is also what makes a
third-party profile a reasonable pull request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

__all__ = ["ApiProfile", "SHEETS"]

# Sheets custom verbs that read data despite being POSTed. Verified against the
# published discovery document; see the project notes for the full table.
_SHEETS_READ_VERBS = frozenset(
    {
        "batchget",
        "batchgetbydatafilter",
        "getbydatafilter",
        "search",
    }
)

_READ_HTTP_METHODS = frozenset({"GET", "HEAD"})


def _trailing_verb(path: str) -> str:
    """Return whatever follows the last colon of the final path segment.

    Google spells non-CRUD operations as ``.../values:batchGet``, and the verb
    is what identifies the operation -- the HTTP method does not, since several
    reads are POSTed because they carry a request body.

    The catch is that A1 notation uses colons too, so ``/values/A1:B2`` yields
    ``"b2"`` here. That is why the caller only trusts this result when it
    matches a verb it knows, and falls back on the HTTP method otherwise.
    """
    tail = path.rsplit("/", 1)[-1]
    _, separator, verb = tail.rpartition(":")
    return verb.lower() if separator else ""


def resolve_sheets(http_method: str, path: str) -> tuple[str, int]:
    """Classify a Sheets API call into a quota bucket and its cost.

    Sheets counts requests, so the cost is always 1 -- including batch calls,
    which Google charges as a single request no matter how many sub-requests
    they carry.

    Reads and writes are metered separately, so misfiling one costs real
    throughput: a read charged to the write bucket consumes one of only 60
    writes a minute.

    Classification recognises the handful of Sheets verbs that read despite
    being POSTed, and otherwise trusts the HTTP method. Anything unrecognised
    therefore lands wherever its verb says, which for a new POST method means
    the write bucket -- the conservative direction to be wrong in.
    """
    if _trailing_verb(path) in _SHEETS_READ_VERBS:
        return "read", 1
    scope = "read" if http_method.upper() in _READ_HTTP_METHODS else "write"
    return scope, 1


@dataclass(frozen=True)
class ApiProfile:
    """Everything the limiter needs to know about one Google API.

    Attributes:
        name: Short label, used in bucket names and error messages.
        host: The API host. Requests to any other host are left alone, so the
            token refresh traffic to ``oauth2.googleapis.com`` never eats the
            quota of the API being limited.
        limits: Bucket name to maximum units per window. Separate buckets are
            what keep reads from starving writes.
        resolve: Maps ``(http_method, path)`` to ``(bucket, cost)``.
    """

    name: str
    host: str
    limits: Mapping[str, int]
    resolve: Callable[[str, str], tuple[str, int]]

    def __post_init__(self) -> None:
        if not self.limits:
            raise ValueError(f"{self.name}: profile defines no limits")
        for bucket, limit in self.limits.items():
            if limit <= 0:
                raise ValueError(
                    f"{self.name}: limit for {bucket!r} must be positive, got {limit!r}"
                )
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    def with_limits(self, **overrides: int) -> ApiProfile:
        """Return a copy with some limits replaced.

        Quotas are not a property of the API alone: they depend on the project,
        on when it was created, and Google revises them (Gmail's changed in May
        2026). Whatever this library ships as a default will eventually be
        wrong for somebody, so overriding has to be a first-class operation
        rather than a workaround.

            SHEETS.with_limits(read=300, write=300)

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


#: Google Sheets API v4.
#:
#: The defaults are the per-user, per-project, per-minute quotas, which is what
#: a single script actually runs into. The per-project ceiling is 300 for each
#: bucket; a job that legitimately owns the whole project can say so with
#: ``SHEETS.with_limits(read=300, write=300)``.
SHEETS = ApiProfile(
    name="sheets",
    host="sheets.googleapis.com",
    limits={"read": 60, "write": 60},
    resolve=resolve_sheets,
)
