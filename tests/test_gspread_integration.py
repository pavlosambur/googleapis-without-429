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

from googleapis_without_429 import SHEETS, RateLimitedSession

from .conftest import FakeClock

SPREADSHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
SHEETS_HOST = "sheets.googleapis.com"


def json_response(payload: dict, status: int = 200) -> requests.Response:
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
    def install(payloads: list[dict]) -> list[str]:
        sent: list[str] = []
        stream = iter(payloads)

        def fake_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
            sent.append(f"{request.method} {request.url}")
            return json_response(next(stream))

        monkeypatch.setattr(requests.Session, "send", fake_send)
        return sent

    return install


@pytest.fixture
def session(clock: FakeClock) -> RateLimitedSession:
    return RateLimitedSession(
        AnonymousCredentials(),
        [SHEETS],
        clock=clock.time,
        sleeper=clock.sleep,
    )


def bucket(session: RateLimitedSession, name: str):
    return session._buckets[(SHEETS_HOST, name)]


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
        assert bucket(session, "read").used == 1
        assert bucket(session, "write").used == 0

    def test_appending_a_row_charges_the_write_quota(
        self, session: RateLimitedSession, stub_transport
    ) -> None:
        """The hot path of the README example: a plain append_row call."""
        stub_transport([SPREADSHEET_PAYLOAD, SPREADSHEET_PAYLOAD, APPEND_PAYLOAD])
        client = gspread.authorize(AnonymousCredentials(), session=session)

        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        spreadsheet.sheet1.append_row(["a", "b"])

        assert bucket(session, "write").used == 1


class TestCoverageBoundary:
    """gspread is not one API. Some of it is Drive, and this profile is not."""

    def test_creating_a_spreadsheet_goes_to_drive_and_is_not_throttled(
        self, session: RateLimitedSession, stub_transport
    ) -> None:
        """A documented limit, not an oversight: gc.create() is a Drive call.

        Drive operations in gspread are one-off (create, delete, and looking a
        sheet up by title); the loop that actually exhausts a quota is Sheets.
        Covering Drive is deferred, so this test pins down what today's profile
        does and does not touch.
        """
        sent = stub_transport([{"id": SPREADSHEET_ID}, SPREADSHEET_PAYLOAD])
        client = gspread.authorize(AnonymousCredentials(), session=session)

        client.create("My Sheet")

        assert any("www.googleapis.com/drive" in call for call in sent)
        assert bucket(session, "write").used == 0, "the Drive call was not metered"
        assert bucket(session, "read").used == 1, "but the Sheets read that follows is"

    def test_opening_by_title_searches_drive_first(
        self, session: RateLimitedSession, stub_transport
    ) -> None:
        """gc.open("name") is a Drive search plus a Sheets read."""
        sent = stub_transport(
            [{"files": [{"id": SPREADSHEET_ID, "name": "My Sheet"}]},
             SPREADSHEET_PAYLOAD]
        )
        client = gspread.authorize(AnonymousCredentials(), session=session)

        client.open("My Sheet")

        assert any("www.googleapis.com/drive" in call for call in sent)
        assert bucket(session, "read").used == 1

    def test_a_loop_of_reads_throttles_without_the_caller_doing_anything(
        self, clock: FakeClock, stub_transport
    ) -> None:
        """The scenario from the README: an ordinary loop, no code changes."""
        stub_transport([SPREADSHEET_PAYLOAD for _ in range(4)])
        limited = RateLimitedSession(
            AnonymousCredentials(),
            [SHEETS.with_limits(read=3, write=3)],
            clock=clock.time,
            sleeper=clock.sleep,
        )
        client = gspread.authorize(AnonymousCredentials(), session=limited)

        for _ in range(4):
            client.open_by_key(SPREADSHEET_ID)

        assert clock.now == pytest.approx(60.0), "the fourth read waited its turn"
