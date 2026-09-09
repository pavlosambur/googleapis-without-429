"""End-to-end test through gspread itself.

The library's central claim is that adopting it changes one line of a user's
program. Testing the session in isolation cannot show that -- only driving a
real gspread client can. The transport is stubbed, so no network is involved.
"""

from __future__ import annotations

import json

import gspread
import pytest
import requests
from google.auth.credentials import AnonymousCredentials

from googleapis_without_429 import DRIVE, SHEETS, RateLimitedSession

from .conftest import FakeClock

SPREADSHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"


def json_response(payload: dict[str, object], status: int = 200) -> requests.Response:
    made = requests.Response()
    made.status_code = status
    made._content = json.dumps(payload).encode()
    made.headers["Content-Type"] = "application/json"
    return made


APPEND_PAYLOAD = {
    "spreadsheetId": SPREADSHEET_ID,
    "updates": {"updatedRows": 1, "updatedCells": 2},
}

SPREADSHEET_PAYLOAD = {
    "spreadsheetId": SPREADSHEET_ID,
    "properties": {"title": "My Sheet"},
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "Sheet1",
                "index": 0,
                "gridProperties": {"rowCount": 100, "columnCount": 20},
            }
        }
    ],
}


@pytest.fixture
def stub_transport(monkeypatch: pytest.MonkeyPatch):
    def install(payloads: list[dict[str, object]]) -> list[str]:
        sent: list[str] = []
        stream = iter(payloads)

        def fake_send(self, request, **kwargs):
            sent.append(f"{request.method} {request.url}")
            return json_response(next(stream))

        monkeypatch.setattr(requests.Session, "send", fake_send)
        return sent

    return install


@pytest.fixture
def session(clock: FakeClock) -> RateLimitedSession:
    return RateLimitedSession(
        AnonymousCredentials(),
        [SHEETS, DRIVE],
        clock=clock.time,
        sleeper=clock.sleep,
    )


PROFILES = {"sheets": SHEETS, "drive": DRIVE}


def bucket(session: RateLimitedSession, profile: str, name: str):
    return session.limiter.bucket(PROFILES[profile], name)


class TestDropInAdoption:
    def test_gspread_uses_the_session_it_is_given(
        self, session: RateLimitedSession
    ) -> None:
        client = gspread.authorize(AnonymousCredentials(), session=session)

        assert client.http_client.session is session

    def test_gspread_accepts_none_credentials_as_its_docs_suggest(
        self, session: RateLimitedSession
    ) -> None:
        """gspread documents passing None when supplying your own session."""
        client = gspread.authorize(None, session=session)  # type: ignore[arg-type]

        assert client.http_client.session is session

    def test_reading_a_spreadsheet_charges_the_read_quota(
        self, session: RateLimitedSession, stub_transport
    ) -> None:
        sent = stub_transport([SPREADSHEET_PAYLOAD])
        client = gspread.authorize(AnonymousCredentials(), session=session)

        spreadsheet = client.open_by_key(SPREADSHEET_ID)

        assert spreadsheet.title == "My Sheet"
        assert any(SPREADSHEET_ID in call for call in sent)
        assert bucket(session, "sheets", "read").used == 1
        assert bucket(session, "sheets", "write").used == 0

    def test_appending_a_row_charges_the_write_quota(
        self, session: RateLimitedSession, stub_transport
    ) -> None:
        """The hot path of the README example: a plain append_row call."""
        stub_transport([SPREADSHEET_PAYLOAD, SPREADSHEET_PAYLOAD, APPEND_PAYLOAD])
        client = gspread.authorize(AnonymousCredentials(), session=session)

        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        spreadsheet.sheet1.append_row(["a", "b"])

        assert bucket(session, "sheets", "write").used == 1


class TestBothApis:
    """gspread is not one API. Sheets moves the data; Drive owns the file."""

    def test_creating_a_spreadsheet_charges_the_drive_quota(
        self, session: RateLimitedSession, stub_transport
    ) -> None:
        """gc.create() is a Drive edit, then a Sheets read of the new file."""
        sent = stub_transport([{"id": SPREADSHEET_ID}, SPREADSHEET_PAYLOAD])
        client = gspread.authorize(AnonymousCredentials(), session=session)

        client.create("My Sheet")

        assert any("www.googleapis.com/drive" in call for call in sent)
        assert bucket(session, "drive", "units").used == 50, "an edit costs 50 units"
        assert bucket(session, "sheets", "read").used == 1
        assert bucket(session, "sheets", "write").used == 0

    def test_opening_by_title_is_a_drive_search_then_a_sheets_read(
        self, session: RateLimitedSession, stub_transport
    ) -> None:
        """A list costs 100 units -- twenty times a plain item read."""
        sent = stub_transport(
            [
                {"files": [{"id": SPREADSHEET_ID, "name": "My Sheet"}]},
                SPREADSHEET_PAYLOAD,
            ]
        )
        client = gspread.authorize(AnonymousCredentials(), session=session)

        client.open("My Sheet")

        assert any("www.googleapis.com/drive" in call for call in sent)
        assert bucket(session, "drive", "units").used == 100
        assert bucket(session, "sheets", "read").used == 1

    def test_the_two_apis_do_not_share_a_quota(
        self, clock: FakeClock, stub_transport
    ) -> None:
        """Exhausting Sheets must not stall Drive, and the reverse."""
        stub_transport([SPREADSHEET_PAYLOAD, SPREADSHEET_PAYLOAD, {"files": []}])
        limited = RateLimitedSession(
            AnonymousCredentials(),
            [SHEETS.with_limits(read=2), DRIVE],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        client = gspread.authorize(AnonymousCredentials(), session=limited)

        client.open_by_key(SPREADSHEET_ID)
        client.open_by_key(SPREADSHEET_ID)
        client.list_spreadsheet_files()  # Drive, with the Sheets read quota spent

        assert clock.slept == []
