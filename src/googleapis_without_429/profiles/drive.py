"""Google Drive API v3: weighted quota units in one shared bucket."""

from __future__ import annotations

import re

from googleapis_without_429.profiles.base import READ_HTTP_METHODS, ApiProfile
from googleapis_without_429.profiles.matching import PathTable

__all__ = [
    "DRIVE",
    "DRIVE_DOWNLOAD_COST",
    "DRIVE_EDIT_COST",
    "DRIVE_LIST_COST",
    "DRIVE_OTHER_COST",
    "DRIVE_READ_COST",
    "EXPLICIT_COSTS",
    "resolve_drive",
]

_VERSION_SEGMENT = re.compile(r"v\d+(?:beta\d*)?")


#: Quota unit costs, from the published table of Drive API usage limits.
DRIVE_READ_COST = 5
DRIVE_LIST_COST = 100
DRIVE_DOWNLOAD_COST = 200
DRIVE_EDIT_COST = 50
#: Cost of an action the table files under "other", such as generating ids.
DRIVE_OTHER_COST = 5


#: Methods the published cost table names explicitly, where the shape of the
#: path gives the wrong answer.
#:
#: ``files.download`` is the one that matters: it is a POST, so the fallback
#: below prices it as an edit at 50 units, while the documented category for it
#: is a download at 200. Under-counting by a factor of four is how a quota is
#: passed without noticing.
#:
#: Paths are relative to ``/drive/v3/``.
EXPLICIT_COSTS: dict[tuple[str, str], int] = {
    ("POST", "files/{fileId}/download"): DRIVE_DOWNLOAD_COST,
    ("GET", "files/{fileId}/export"): DRIVE_DOWNLOAD_COST,
    ("GET", "files/generateIds"): DRIVE_OTHER_COST,
}

_TABLE = PathTable(EXPLICIT_COSTS)


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

    explicit = _TABLE.lookup(http_method, segments)
    if explicit is not None:
        return "units", explicit

    if http_method.upper() not in READ_HTTP_METHODS:
        # Creating, updating, copying, deleting and uploading are all edits.
        return "units", DRIVE_EDIT_COST

    if (segments and segments[-1] == "export") or "alt=media" in query:
        return "units", DRIVE_DOWNLOAD_COST

    # Odd number of segments means a collection, even means one item.
    is_collection = len(segments) % 2 == 1
    return "units", DRIVE_LIST_COST if is_collection else DRIVE_READ_COST


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
