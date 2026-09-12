"""Tests for the path matcher shared by the Gmail and Drive profiles."""

from __future__ import annotations

import pytest

from googleapis_without_429.profiles.matching import PathTable, path_segments


class TestPathSegments:
    def test_it_splits_a_plain_path(self) -> None:
        assert path_segments("/drive/v3/files/id7") == ["drive", "v3", "files", "id7"]

    def test_it_ignores_empty_segments(self) -> None:
        assert path_segments("//files//id7//") == ["files", "id7"]

    def test_an_empty_path_yields_nothing(self) -> None:
        assert path_segments("/") is None

    def test_it_drops_everything_up_to_and_past_the_marker(self) -> None:
        """Gmail paths carry users/{userId} before the part that identifies
        the method."""
        assert path_segments("/gmail/v1/users/me/labels", after="users") == ["labels"]

    def test_a_path_without_the_marker_yields_nothing(self) -> None:
        assert path_segments("/gmail/v1/nonsense", after="users") is None

    def test_a_path_ending_at_the_marker_yields_nothing(self) -> None:
        assert path_segments("/gmail/v1/users/me", after="users") is None


class TestPathTable:
    def test_a_literal_template_matches_exactly(self) -> None:
        table = PathTable({("GET", "labels"): 1})
        assert table.lookup("GET", ["labels"]) == 1
        assert table.lookup("GET", ["threads"]) is None

    def test_a_placeholder_matches_any_segment(self) -> None:
        table = PathTable({("GET", "messages/{id}"): 20})
        assert table.lookup("GET", ["messages", "18f2a"]) == 20
        assert table.lookup("GET", ["messages", "anything"]) == 20

    def test_the_http_method_must_match(self) -> None:
        table = PathTable({("GET", "files"): 100})
        assert table.lookup("POST", ["files"]) is None

    def test_the_http_method_is_case_insensitive(self) -> None:
        table = PathTable({("GET", "files"): 100})
        assert table.lookup("get", ["files"]) == 100

    def test_the_segment_count_must_match(self) -> None:
        table = PathTable({("GET", "files/{id}"): 5})
        assert table.lookup("GET", ["files"]) is None
        assert table.lookup("GET", ["files", "id7", "extra"]) is None

    def test_a_custom_verb_matches_only_that_suffix(self) -> None:
        table = PathTable({("POST", "keypairs/{id}:disable"): 7})
        assert table.lookup("POST", ["keypairs", "kp1:disable"]) == 7
        assert table.lookup("POST", ["keypairs", "kp1:enable"]) is None
        assert table.lookup("POST", ["keypairs", "kp1"]) is None

    def test_a_literal_beats_a_placeholder_of_the_same_shape(self) -> None:
        """Otherwise a file whose id is 'download' is priced as the download."""
        table = PathTable(
            {
                ("POST", "files/{fileId}/download"): 200,
                ("POST", "files/{fileId}/{action}"): 50,
            }
        )
        assert table.lookup("POST", ["files", "id7", "download"]) == 200
        assert table.lookup("POST", ["files", "id7", "copy"]) == 50

    def test_an_empty_table_matches_nothing(self) -> None:
        assert PathTable({}).lookup("GET", ["files"]) is None

    def test_len_counts_the_entries(self) -> None:
        table = PathTable({("GET", "a"): 1, ("POST", "a"): 2, ("GET", "a/{b}"): 3})
        assert len(table) == 3

    @pytest.mark.parametrize("segments", [[], ["a"], ["a", "b", "c", "d", "e"]])
    def test_unknown_shapes_return_none_rather_than_raising(
        self, segments: list[str]
    ) -> None:
        table = PathTable({("GET", "files/{id}"): 5})
        assert table.lookup("GET", segments) is None
