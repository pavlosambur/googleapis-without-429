"""API profiles: which host, which quota buckets, and what a call costs.

A profile is data, not behaviour. Adding support for another Google API means
adding one of these, not refactoring anything -- which is also what makes a
third-party profile a reasonable pull request.

The two profiles shipped here look different on purpose. Sheets counts requests
in separate read and write buckets; Drive counts weighted quota units in a
single shared one. Both fall out of the same structure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

__all__ = ["DRIVE", "SHEETS", "ApiProfile", "resolve_drive", "resolve_sheets"]

#: What a profile's ``resolve`` is called with: HTTP method, URL path, URL query.
#: The query string is needed because Drive signals a download with ``alt=media``
#: rather than with a distinct path.
Resolver = Callable[[str, str, str], "tuple[str, int]"]

_READ_HTTP_METHODS = frozenset({"GET", "HEAD"})
_VERSION_SEGMENT = re.compile(r"v\d+(?:beta\d*)?")


# --------------------------------------------------------------------------
# Sheets
# --------------------------------------------------------------------------

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


def resolve_sheets(http_method: str, path: str, query: str = "") -> tuple[str, int]:
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


# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------

#: Quota unit costs, from the published table of Drive API usage limits.
DRIVE_READ_COST = 5
DRIVE_LIST_COST = 100
DRIVE_DOWNLOAD_COST = 200
DRIVE_EDIT_COST = 50


def _drive_resource_path(path: str) -> list[str]:
    """Strip the ``/upload`` marker, the API name and the version segment.

    ``/drive/v3/files/abc/permissions`` becomes ``["files", "abc",
    "permissions"]``, which is what identifies the resource being addressed.
    """
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "upload":
        parts = parts[1:]
    if parts and parts[0] == "drive":
        parts = parts[1:]
    if parts and _VERSION_SEGMENT.fullmatch(parts[0]):
        parts = parts[1:]
    return parts


def resolve_drive(http_method: str, path: str, query: str = "") -> tuple[str, int]:
    """Classify a Drive API call and price it in quota units.

    Drive meters everything against one pool, but charges wildly different
    amounts: listing costs 100 units where fetching one item costs 5, and
    downloading content costs 200. Counting calls instead of units would be
    off by a factor of forty between the cheapest and dearest request.

    Reads are priced by what the path addresses. A path ending in a collection
    (``/files``, ``/files/{id}/permissions``) is a list; one ending in a
    specific item (``/files/{id}``) is a read. Google's REST paths alternate
    collection and item, so the number of segments settles it.

    Downloads are recognised by ``alt=media`` in the query string or by an
    ``/export`` suffix -- Drive does not give them a path of their own.
    """
    segments = _drive_resource_path(path)

    if http_method.upper() not in _READ_HTTP_METHODS:
        # Creating, updating, copying, deleting and uploading are all edits.
        return "units", DRIVE_EDIT_COST

    if (segments and segments[-1] == "export") or "alt=media" in query:
        return "units", DRIVE_DOWNLOAD_COST

    # Odd number of segments means a collection, even means one item.
    is_collection = len(segments) % 2 == 1
    return "units", DRIVE_LIST_COST if is_collection else DRIVE_READ_COST


# --------------------------------------------------------------------------
# The profile structure
# --------------------------------------------------------------------------


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
    """

    name: str
    host: str
    limits: Mapping[str, int]
    resolve: Resolver
    path_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.limits:
            raise ValueError(f"{self.name}: profile defines no limits")
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

        Quotas are not a property of the API alone: they depend on the project,
        on when it was created, and Google revises them. Drive's limits changed
        on 1 May 2026, and projects that were already using it kept the old
        ones -- expressed in a different unit, so no conversion exists.
        Whatever this library ships as a default is therefore wrong for
        somebody, and overriding has to be a first-class operation::

            SHEETS.with_limits(read=300, write=300)
            DRIVE.with_limits(units=12_000)

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

#: Google Drive API v3 (and the v2 upload endpoint).
#:
#: The default is the per-user, per-project, per-minute quota for projects
#: created on or after 1 May 2026. An older project runs on the previous,
#: differently-measured quota, and should say so::
#:
#:     DRIVE.with_limits(units=...)
#:
#: This profile matters even for code that only means to use Sheets: gspread
#: reaches Drive to create a spreadsheet, delete one, share one, or find one
#: by title.
DRIVE = ApiProfile(
    name="drive",
    host="www.googleapis.com",
    limits={"units": 325_000},
    resolve=resolve_drive,
    path_prefixes=("/drive/", "/upload/drive/"),
)
