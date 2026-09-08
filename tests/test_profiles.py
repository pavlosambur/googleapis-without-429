"""Tests for API profiles and Sheets scope classification."""

from __future__ import annotations

import pytest

from googleapis_without_429.profiles import SHEETS, ApiProfile, resolve_sheets

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
