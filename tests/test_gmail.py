"""Tests for the Gmail profile.

Gmail is where guessing a cost stops working: ``messages.get`` costs twenty
units and ``messages.list`` costs five, and nothing about either path says so.
So every method the API publishes is checked here, not a sample -- the table is
hand-assembled from two Google sources and a single transposed row would
silently overcharge or, worse, undercharge a quota.
"""

from __future__ import annotations

import pytest

from googleapis_without_429 import DRIVE, GMAIL, SHEETS, QuotaLimiter
from googleapis_without_429.profiles.gmail import (
    GMAIL_UNPRICED_COST,
    METHOD_COSTS,
    resolve_gmail,
)

from .conftest import FakeClock

USER = "/gmail/v1/users/me/"

# Every method in the Gmail v1 discovery document (revision 20260903), with the
# cost Google publishes for it. The sixteen priced at 100 without a documented
# figure are the settings.cse and smimeInfo families, which fall through to
# GMAIL_UNPRICED_COST.
# (method id, HTTP method, path below users/{userId}, expected units)
GMAIL_METHODS: list[tuple[str, str, str, int]] = [
    ("drafts.create", "POST", "drafts", 10),
    ("drafts.delete", "DELETE", "drafts/id7", 10),
    ("drafts.get", "GET", "drafts/id7", 20),
    ("drafts.list", "GET", "drafts", 5),
    ("drafts.send", "POST", "drafts/send", 100),
    ("drafts.update", "PUT", "drafts/id7", 15),
    ("getProfile", "GET", "profile", 1),
    ("history.list", "GET", "history", 2),
    ("labels.create", "POST", "labels", 5),
    ("labels.delete", "DELETE", "labels/id7", 5),
    ("labels.get", "GET", "labels/id7", 1),
    ("labels.list", "GET", "labels", 1),
    ("labels.patch", "PATCH", "labels/id7", 5),
    ("labels.update", "PUT", "labels/id7", 5),
    ("messages.attachments.get", "GET", "messages/id7/attachments/id7", 20),
    ("messages.batchDelete", "POST", "messages/batchDelete", 50),
    ("messages.batchModify", "POST", "messages/batchModify", 50),
    ("messages.delete", "DELETE", "messages/id7", 10),
    ("messages.get", "GET", "messages/id7", 20),
    ("messages.import", "POST", "messages/import", 25),
    ("messages.insert", "POST", "messages", 25),
    ("messages.list", "GET", "messages", 5),
    ("messages.modify", "POST", "messages/id7/modify", 5),
    ("messages.send", "POST", "messages/send", 100),
    ("messages.trash", "POST", "messages/id7/trash", 20),
    ("messages.untrash", "POST", "messages/id7/untrash", 5),
    ("settings.cse.identities.create", "POST", "settings/cse/identities", 100),
    (
        "settings.cse.identities.delete",
        "DELETE",
        "settings/cse/identities/a%40e.co",
        100,
    ),
    ("settings.cse.identities.get", "GET", "settings/cse/identities/a%40e.co", 100),
    ("settings.cse.identities.list", "GET", "settings/cse/identities", 100),
    ("settings.cse.identities.patch", "PATCH", "settings/cse/identities/a%40e.co", 100),
    ("settings.cse.keypairs.create", "POST", "settings/cse/keypairs", 100),
    ("settings.cse.keypairs.disable", "POST", "settings/cse/keypairs/id7:disable", 100),
    ("settings.cse.keypairs.enable", "POST", "settings/cse/keypairs/id7:enable", 100),
    ("settings.cse.keypairs.get", "GET", "settings/cse/keypairs/id7", 100),
    ("settings.cse.keypairs.list", "GET", "settings/cse/keypairs", 100),
    (
        "settings.cse.keypairs.obliterate",
        "POST",
        "settings/cse/keypairs/id7:obliterate",
        100,
    ),
    ("settings.delegates.create", "POST", "settings/delegates", 100),
    ("settings.delegates.delete", "DELETE", "settings/delegates/a%40e.co", 5),
    ("settings.delegates.get", "GET", "settings/delegates/a%40e.co", 1),
    ("settings.delegates.list", "GET", "settings/delegates", 1),
    ("settings.filters.create", "POST", "settings/filters", 5),
    ("settings.filters.delete", "DELETE", "settings/filters/id7", 5),
    ("settings.filters.get", "GET", "settings/filters/id7", 1),
    ("settings.filters.list", "GET", "settings/filters", 1),
    (
        "settings.forwardingAddresses.create",
        "POST",
        "settings/forwardingAddresses",
        100,
    ),
    (
        "settings.forwardingAddresses.delete",
        "DELETE",
        "settings/forwardingAddresses/a%40e.co",
        5,
    ),
    (
        "settings.forwardingAddresses.get",
        "GET",
        "settings/forwardingAddresses/a%40e.co",
        1,
    ),
    ("settings.forwardingAddresses.list", "GET", "settings/forwardingAddresses", 1),
    ("settings.getAutoForwarding", "GET", "settings/autoForwarding", 1),
    ("settings.getImap", "GET", "settings/imap", 1),
    ("settings.getLanguage", "GET", "settings/language", 1),
    ("settings.getPop", "GET", "settings/pop", 1),
    ("settings.getVacation", "GET", "settings/vacation", 1),
    ("settings.sendAs.create", "POST", "settings/sendAs", 100),
    ("settings.sendAs.delete", "DELETE", "settings/sendAs/a%40e.co", 5),
    ("settings.sendAs.get", "GET", "settings/sendAs/a%40e.co", 1),
    ("settings.sendAs.list", "GET", "settings/sendAs", 1),
    ("settings.sendAs.patch", "PATCH", "settings/sendAs/a%40e.co", 100),
    (
        "settings.sendAs.smimeInfo.delete",
        "DELETE",
        "settings/sendAs/a%40e.co/smimeInfo/id7",
        100,
    ),
    (
        "settings.sendAs.smimeInfo.get",
        "GET",
        "settings/sendAs/a%40e.co/smimeInfo/id7",
        100,
    ),
    (
        "settings.sendAs.smimeInfo.insert",
        "POST",
        "settings/sendAs/a%40e.co/smimeInfo",
        100,
    ),
    (
        "settings.sendAs.smimeInfo.list",
        "GET",
        "settings/sendAs/a%40e.co/smimeInfo",
        100,
    ),
    (
        "settings.sendAs.smimeInfo.setDefault",
        "POST",
        "settings/sendAs/a%40e.co/smimeInfo/id7/setDefault",
        100,
    ),
    ("settings.sendAs.update", "PUT", "settings/sendAs/a%40e.co", 100),
    ("settings.sendAs.verify", "POST", "settings/sendAs/a%40e.co/verify", 100),
    ("settings.updateAutoForwarding", "PUT", "settings/autoForwarding", 5),
    ("settings.updateImap", "PUT", "settings/imap", 5),
    ("settings.updateLanguage", "PUT", "settings/language", 5),
    ("settings.updatePop", "PUT", "settings/pop", 100),
    ("settings.updateVacation", "PUT", "settings/vacation", 5),
    ("stop", "POST", "stop", 50),
    ("threads.delete", "DELETE", "threads/id7", 20),
    ("threads.get", "GET", "threads/id7", 40),
    ("threads.list", "GET", "threads", 10),
    ("threads.modify", "POST", "threads/id7/modify", 10),
    ("threads.trash", "POST", "threads/id7/trash", 20),
    ("threads.untrash", "POST", "threads/id7/untrash", 10),
    ("watch", "POST", "watch", 100),
]


class TestEveryPublishedMethod:
    @pytest.mark.parametrize(
        ("http_method", "path", "expected"),
        [(verb, path, cost) for _, verb, path, cost in GMAIL_METHODS],
        ids=[name for name, *_ in GMAIL_METHODS],
    )
    def test_each_method_is_priced_correctly(
        self, http_method: str, path: str, expected: int
    ) -> None:
        bucket, cost = resolve_gmail(http_method, USER + path)
        assert bucket == "units", "Gmail meters everything against one pool"
        assert cost == expected

    def test_the_table_covers_the_whole_api(self) -> None:
        """A guard against the case list falling behind the API."""
        assert len(GMAIL_METHODS) == 79
        assert len(METHOD_COSTS) == 63


class TestThePricesThatMatterMost:
    def test_reading_a_message_costs_four_times_listing_them(self) -> None:
        """The pair a path-shape heuristic would get wrong."""
        _, listing = resolve_gmail("GET", USER + "messages")
        _, reading = resolve_gmail("GET", USER + "messages/id7")
        assert (listing, reading) == (5, 20)

    def test_sending_costs_a_hundred_times_a_label_read(self) -> None:
        """Why counting requests instead of units would be meaningless here."""
        _, label = resolve_gmail("GET", USER + "labels/id7")
        _, send = resolve_gmail("POST", USER + "messages/send")
        assert (label, send) == (1, 100)

    def test_a_thread_read_is_dearer_than_a_message_read(self) -> None:
        _, thread = resolve_gmail("GET", USER + "threads/id7")
        _, message = resolve_gmail("GET", USER + "messages/id7")
        assert thread > message


class TestUnknownPaths:
    def test_a_method_added_after_this_table_is_charged_the_maximum(self) -> None:
        """Overcharging costs throughput; undercharging costs a 429."""
        assert resolve_gmail("GET", USER + "brandNewThing")[1] == GMAIL_UNPRICED_COST

    def test_an_unrecognised_shape_is_charged_the_maximum(self) -> None:
        assert resolve_gmail("GET", "/gmail/v1/nonsense")[1] == GMAIL_UNPRICED_COST

    def test_a_bare_user_path_is_charged_the_maximum(self) -> None:
        assert resolve_gmail("GET", "/gmail/v1/users/me")[1] == GMAIL_UNPRICED_COST

    def test_an_unknown_http_method_is_charged_the_maximum(self) -> None:
        assert resolve_gmail("TRACE", USER + "labels")[1] == GMAIL_UNPRICED_COST

    def test_the_http_method_is_case_insensitive(self) -> None:
        assert resolve_gmail("get", USER + "labels")[1] == 1


class TestLiteralsBeatPlaceholders:
    def test_a_draft_identified_as_send_is_still_priced_as_a_draft_id(self) -> None:
        """Specificity ordering: a literal segment must win over a placeholder.

        Gmail has no POST on drafts/{id} today, so nothing collides; this pins
        the ordering so a future entry cannot quietly reverse it.
        """
        _, send = resolve_gmail("POST", USER + "drafts/send")
        assert send == 100, "drafts.send, not a draft whose id is 'send'"

    def test_a_custom_verb_suffix_is_matched(self) -> None:
        path = USER + "settings/cse/keypairs/kp1:disable"
        assert resolve_gmail("POST", path)[1] == GMAIL_UNPRICED_COST


class TestTheProfile:
    def test_it_defaults_to_the_per_user_quota(self) -> None:
        assert dict(GMAIL.limits) == {"units": 6_000}

    def test_it_claims_its_own_host(self) -> None:
        assert GMAIL.claims("gmail.googleapis.com", "/gmail/v1/users/me/labels")
        assert not GMAIL.claims("sheets.googleapis.com", "/v4/spreadsheets/x")

    def test_the_project_ceiling_can_be_set(self) -> None:
        assert dict(GMAIL.with_limits(units=1_200_000).limits) == {"units": 1_200_000}

    def test_sending_mail_in_a_loop_throttles(self, clock: FakeClock) -> None:
        """Sixty sends is the whole per-user minute: 60 x 100 = 6000 units."""
        limiter = QuotaLimiter([GMAIL], clock=clock.time, sleeper=clock.sleep)
        url = "https://gmail.googleapis.com" + USER + "messages/send"

        for _ in range(60):
            limiter.acquire_for("POST", url)
        assert clock.slept == []

        limiter.acquire_for("POST", url)
        assert clock.now == pytest.approx(60.0), "the sixty-first send waited"

    def test_it_coexists_with_the_other_profiles(self, clock: FakeClock) -> None:
        limiter = QuotaLimiter(
            [SHEETS, DRIVE, GMAIL], clock=clock.time, sleeper=clock.sleep
        )
        limiter.acquire_for("GET", "https://gmail.googleapis.com" + USER + "labels")
        limiter.acquire_for("GET", "https://sheets.googleapis.com/v4/spreadsheets/x")

        assert limiter.bucket(GMAIL, "units").used == 1
        assert limiter.bucket(SHEETS, "read").used == 1
