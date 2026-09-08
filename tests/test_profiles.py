"""Tests for API profiles and Sheets scope classification."""

from __future__ import annotations

import pytest

from googleapis_without_429.profiles import (
    DRIVE,
    SHEETS,
    ApiProfile,
    resolve_drive,
    resolve_sheets,
)

SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"

# Every method of Sheets API v4, taken from the published discovery document
# (revision 20260831) with path placeholders filled in. The expected bucket is
# the operation's real nature, which the HTTP verb alone does not give away.
SHEETS_METHODS = [
    # (label, http_method, path, expected bucket)
    ("spreadsheets.get", "GET", f"/v4/spreadsheets/{SHEET_ID}", "read"),
    ("values.get", "GET", f"/v4/spreadsheets/{SHEET_ID}/values/A1:B2", "read"),
    ("values.batchGet", "GET", f"/v4/spreadsheets/{SHEET_ID}/values:batchGet", "read"),
    (
        "developerMetadata.get",
        "GET",
        f"/v4/spreadsheets/{SHEET_ID}/developerMetadata/42",
        "read",
    ),
    # POST, but reads. These are the ones a verb-only rule gets wrong.
    (
        "spreadsheets.getByDataFilter",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}:getByDataFilter",
        "read",
    ),
    (
        "values.batchGetByDataFilter",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values:batchGetByDataFilter",
        "read",
    ),
    (
        "developerMetadata.search",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/developerMetadata:search",
        "read",
    ),
    ("spreadsheets.create", "POST", "/v4/spreadsheets", "write"),
    (
        "spreadsheets.batchUpdate",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}:batchUpdate",
        "write",
    ),
    (
        "sheets.copyTo",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/sheets/0:copyTo",
        "write",
    ),
    (
        "values.append",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values/A1:B2:append",
        "write",
    ),
    ("values.clear", "POST", f"/v4/spreadsheets/{SHEET_ID}/values/A1:B2:clear", "write"),
    (
        "values.batchUpdate",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values:batchUpdate",
        "write",
    ),
    (
        "values.batchClear",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values:batchClear",
        "write",
    ),
    (
        "values.batchUpdateByDataFilter",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values:batchUpdateByDataFilter",
        "write",
    ),
    (
        "values.batchClearByDataFilter",
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values:batchClearByDataFilter",
        "write",
    ),
    ("values.update", "PUT", f"/v4/spreadsheets/{SHEET_ID}/values/A1:B2", "write"),
]


class TestSheetsResolution:
    @pytest.mark.parametrize(
        ("http_method", "path", "expected"),
        [(m, p, e) for _, m, p, e in SHEETS_METHODS],
        ids=[label for label, *_ in SHEETS_METHODS],
    )
    def test_every_sheets_method_lands_in_the_right_bucket(
        self, http_method: str, path: str, expected: str
    ) -> None:
        bucket, cost = resolve_sheets(http_method, path)
        assert bucket == expected
        assert cost == 1, "Sheets counts requests, so every call costs exactly one"

    def test_the_covered_methods_are_the_whole_api(self) -> None:
        """A guard against the table quietly falling behind the API."""
        assert len(SHEETS_METHODS) == 17

    def test_a_batch_call_costs_one_request_not_one_per_subrequest(self) -> None:
        """Google charges a batch as a single request; overcounting wastes quota."""
        _, cost = resolve_sheets(
            "POST", f"/v4/spreadsheets/{SHEET_ID}/values:batchUpdate"
        )
        assert cost == 1

    def test_an_unrecognised_verb_falls_back_to_the_http_method(self) -> None:
        """A future POST method lands in the write bucket: the safe direction."""
        bucket, _ = resolve_sheets("POST", f"/v4/spreadsheets/{SHEET_ID}:somethingNew")
        assert bucket == "write"

    def test_an_unrecognised_verb_on_a_get_stays_a_read(self) -> None:
        bucket, _ = resolve_sheets("GET", f"/v4/spreadsheets/{SHEET_ID}:somethingNew")
        assert bucket == "read"

    def test_a_range_with_a_quoted_sheet_name_containing_colons(self) -> None:
        """Sheet names may contain colons; the append verb must still be found."""
        path = f"/v4/spreadsheets/{SHEET_ID}/values/'a:b'!A1:B2:append"
        assert resolve_sheets("POST", path)[0] == "write"

    def test_a_colon_inside_a_range_is_not_mistaken_for_a_custom_verb(self) -> None:
        """A1 notation is full of colons: /values/A1:B2 must stay a plain read."""
        bucket, _ = resolve_sheets("GET", f"/v4/spreadsheets/{SHEET_ID}/values/A1:B2")
        assert bucket == "read"

    def test_http_method_is_case_insensitive(self) -> None:
        assert resolve_sheets("get", f"/v4/spreadsheets/{SHEET_ID}")[0] == "read"


class TestProfileLimits:
    def test_sheets_defaults_to_the_per_user_quota(self) -> None:
        assert dict(SHEETS.limits) == {"read": 60, "write": 60}

    def test_limits_are_not_mutable_through_the_profile(self) -> None:
        with pytest.raises(TypeError):
            SHEETS.limits["read"] = 9999  # type: ignore[index]

    def test_with_limits_returns_a_new_profile_and_leaves_the_original(self) -> None:
        raised = SHEETS.with_limits(read=300, write=300)

        assert dict(raised.limits) == {"read": 300, "write": 300}
        assert dict(SHEETS.limits) == {"read": 60, "write": 60}
        assert raised.host == SHEETS.host
        assert raised.resolve is SHEETS.resolve

    def test_with_limits_can_override_a_single_bucket(self) -> None:
        assert dict(SHEETS.with_limits(read=300).limits) == {"read": 300, "write": 60}

    def test_a_misspelled_bucket_is_rejected_rather_than_ignored(self) -> None:
        """Silently dropping `writes=300` would leave the caller falsely reassured."""
        with pytest.raises(ValueError, match="unknown limit"):
            SHEETS.with_limits(writes=300)

    def test_rejects_a_profile_without_limits(self) -> None:
        with pytest.raises(ValueError, match="defines no limits"):
            ApiProfile(name="empty", host="x", limits={}, resolve=resolve_sheets)

    def test_rejects_a_non_positive_limit(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            ApiProfile(
                name="bad", host="x", limits={"read": 0}, resolve=resolve_sheets
            )


DRIVE_FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz"

# Drive prices calls in quota units, from the published limits table.
DRIVE_METHODS = [
    # (label, http_method, path, query, expected cost)
    ("files.list", "GET", "/drive/v3/files", "", 100),
    ("files.get", "GET", f"/drive/v3/files/{DRIVE_FILE_ID}", "", 5),
    (
        "files.get download",
        "GET",
        f"/drive/v3/files/{DRIVE_FILE_ID}",
        "alt=media",
        200,
    ),
    ("files.export", "GET", f"/drive/v3/files/{DRIVE_FILE_ID}/export", "", 200),
    (
        "permissions.list",
        "GET",
        f"/drive/v3/files/{DRIVE_FILE_ID}/permissions",
        "",
        100,
    ),
    (
        "permissions.get",
        "GET",
        f"/drive/v3/files/{DRIVE_FILE_ID}/permissions/p1",
        "",
        5,
    ),
    ("files.create", "POST", "/drive/v3/files", "", 50),
    ("files.update", "PATCH", f"/drive/v3/files/{DRIVE_FILE_ID}", "", 50),
    ("files.delete", "DELETE", f"/drive/v3/files/{DRIVE_FILE_ID}", "", 50),
    ("files.copy", "POST", f"/drive/v3/files/{DRIVE_FILE_ID}/copy", "", 50),
    ("upload v2", "POST", "/upload/drive/v2/files", "uploadType=media", 50),
]


class TestDriveResolution:
    @pytest.mark.parametrize(
        ("http_method", "path", "query", "expected"),
        [(m, p, q, c) for _, m, p, q, c in DRIVE_METHODS],
        ids=[label for label, *_ in DRIVE_METHODS],
    )
    def test_calls_are_priced_in_quota_units(
        self, http_method: str, path: str, query: str, expected: int
    ) -> None:
        bucket, cost = resolve_drive(http_method, path, query)
        assert bucket == "units", "Drive meters everything against one pool"
        assert cost == expected

    def test_listing_costs_twenty_times_a_single_read(self) -> None:
        """Counting calls instead of units would treat these as equal."""
        _, listing = resolve_drive("GET", "/drive/v3/files", "")
        _, reading = resolve_drive("GET", f"/drive/v3/files/{DRIVE_FILE_ID}", "")
        assert listing == reading * 20

    def test_an_unknown_api_version_is_still_understood(self) -> None:
        _, cost = resolve_drive("GET", f"/drive/v4/files/{DRIVE_FILE_ID}", "")
        assert cost == 5


class TestProfileClaims:
    def test_sheets_claims_its_whole_host(self) -> None:
        assert SHEETS.claims("sheets.googleapis.com", "/v4/spreadsheets/x")
        assert SHEETS.claims("sheets.googleapis.com", "/anything/new")

    def test_a_profile_ignores_other_hosts(self) -> None:
        assert not SHEETS.claims("oauth2.googleapis.com", "/token")
        assert not DRIVE.claims("sheets.googleapis.com", "/drive/v3/files")

    def test_drive_claims_only_its_paths_on_a_shared_host(self) -> None:
        """www.googleapis.com serves more than Drive; the rest is not ours."""
        assert DRIVE.claims("www.googleapis.com", "/drive/v3/files")
        assert DRIVE.claims("www.googleapis.com", "/upload/drive/v2/files")
        assert not DRIVE.claims("www.googleapis.com", "/calendar/v3/calendars")
        assert not DRIVE.claims("www.googleapis.com", "/youtube/v3/videos")

    def test_drive_defaults_to_the_current_per_user_quota(self) -> None:
        assert dict(DRIVE.limits) == {"units": 325_000}

    def test_drive_limits_are_overridable_for_an_older_project(self) -> None:
        """Projects predating 1 May 2026 run on a different, older quota."""
        assert dict(DRIVE.with_limits(units=12_000).limits) == {"units": 12_000}
