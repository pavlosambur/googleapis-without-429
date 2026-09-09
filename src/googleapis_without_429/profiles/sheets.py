"""Google Sheets API v4: two buckets, every call costing one request."""

from __future__ import annotations

from googleapis_without_429.profiles.base import READ_HTTP_METHODS, ApiProfile

__all__ = ["SHEETS", "resolve_sheets"]


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


def resolve_sheets(
    http_method: str,
    path: str,
    query: str = "",  # noqa: ARG001 - part of the resolver contract; Drive needs it
) -> tuple[str, int]:
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
    scope = "read" if http_method.upper() in READ_HTTP_METHODS else "write"
    return scope, 1


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
