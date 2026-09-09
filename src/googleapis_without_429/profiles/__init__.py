"""API profiles: which host, which quota buckets, and what a call costs.

The profiles shipped here look different on purpose. Sheets counts requests in
separate read and write buckets; Drive counts weighted quota units in a single
shared one. Both fall out of the same structure.
"""

from googleapis_without_429.profiles.base import (
    READ_HTTP_METHODS,
    ApiProfile,
    Resolver,
)
from googleapis_without_429.profiles.drive import DRIVE, resolve_drive
from googleapis_without_429.profiles.sheets import SHEETS, resolve_sheets

__all__ = [
    "DRIVE",
    "READ_HTTP_METHODS",
    "SHEETS",
    "ApiProfile",
    "Resolver",
    "resolve_drive",
    "resolve_sheets",
]
