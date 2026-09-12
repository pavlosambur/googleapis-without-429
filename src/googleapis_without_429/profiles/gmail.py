"""Google Gmail API v1: weighted quota units, priced per method.

Gmail is the API the weighted core was designed for. Its calls differ by a
factor of a hundred -- ``labels.get`` costs 1 unit and ``messages.send`` costs
100 -- so counting requests here would be meaningless. Guessing a cost from the
shape of the path, which is what the Drive profile does, would also be wrong:
``messages.list`` costs 5 and ``messages.get`` costs 20, and nothing in either
path says so.

The table below is therefore explicit, and had to be assembled from two sources
that Google does not publish together: the usage-limits page gives a cost per
method name, and the discovery document gives the HTTP method and path for each
of those names.
"""

from __future__ import annotations

from googleapis_without_429.profiles.base import ApiProfile
from googleapis_without_429.profiles.matching import PathTable, path_segments

__all__ = ["GMAIL", "GMAIL_UNPRICED_COST", "METHOD_COSTS", "resolve_gmail"]

#: Charged for any method Google has not published a price for -- the whole
#: ``settings.cse`` and ``smimeInfo`` families, sixteen methods in all, plus
#: anything added to the API after this table was written.
#:
#: It is the most any documented method costs, so an unknown call can only be
#: overcharged, never undercharged. Being wrong the other way would mean
#: sailing past the real quota and collecting the 429 this library exists to
#: prevent. Those methods are administrative and are not called in loops, so
#: the overcharge costs nothing in practice.
GMAIL_UNPRICED_COST = 100

#: ``(HTTP method, path below /gmail/v1/users/{userId}/)`` to quota units.
#:
#: Costs from the published usage limits; paths and HTTP methods from the
#: discovery document, revision 20260903. Every Gmail method lives under that
#: prefix, so storing it sixty-three times would be noise.
METHOD_COSTS: dict[tuple[str, str], int] = {
    ("POST", "drafts"): 10,  # drafts.create
    ("DELETE", "drafts/{id}"): 10,  # drafts.delete
    ("GET", "drafts/{id}"): 20,  # drafts.get
    ("GET", "drafts"): 5,  # drafts.list
    ("POST", "drafts/send"): 100,  # drafts.send
    ("PUT", "drafts/{id}"): 15,  # drafts.update
    ("GET", "profile"): 1,  # getProfile
    ("GET", "history"): 2,  # history.list
    ("POST", "labels"): 5,  # labels.create
    ("DELETE", "labels/{id}"): 5,  # labels.delete
    ("GET", "labels/{id}"): 1,  # labels.get
    ("GET", "labels"): 1,  # labels.list
    ("PUT", "labels/{id}"): 5,  # labels.update
    ("GET", "messages/{messageId}/attachments/{id}"): 20,  # messages.attachments.get
    ("POST", "messages/batchDelete"): 50,  # messages.batchDelete
    ("POST", "messages/batchModify"): 50,  # messages.batchModify
    ("DELETE", "messages/{id}"): 10,  # messages.delete
    ("GET", "messages/{id}"): 20,  # messages.get
    ("POST", "messages/import"): 25,  # messages.import
    ("POST", "messages"): 25,  # messages.insert
    ("GET", "messages"): 5,  # messages.list
    ("POST", "messages/{id}/modify"): 5,  # messages.modify
    ("POST", "messages/send"): 100,  # messages.send
    ("POST", "messages/{id}/trash"): 20,  # messages.trash
    ("POST", "messages/{id}/untrash"): 5,  # messages.untrash
    ("POST", "settings/delegates"): 100,  # settings.delegates.create
    ("DELETE", "settings/delegates/{delegateEmail}"): 5,  # settings.delegates.delete
    ("GET", "settings/delegates/{delegateEmail}"): 1,  # settings.delegates.get
    ("GET", "settings/delegates"): 1,  # settings.delegates.list
    ("POST", "settings/filters"): 5,  # settings.filters.create
    ("DELETE", "settings/filters/{id}"): 5,  # settings.filters.delete
    ("GET", "settings/filters/{id}"): 1,  # settings.filters.get
    ("GET", "settings/filters"): 1,  # settings.filters.list
    # settings.forwardingAddresses.create
    ("POST", "settings/forwardingAddresses"): 100,
    # settings.forwardingAddresses.delete
    ("DELETE", "settings/forwardingAddresses/{forwardingEmail}"): 5,
    # settings.forwardingAddresses.get
    ("GET", "settings/forwardingAddresses/{forwardingEmail}"): 1,
    ("GET", "settings/forwardingAddresses"): 1,  # settings.forwardingAddresses.list
    ("GET", "settings/autoForwarding"): 1,  # settings.getAutoForwarding
    ("GET", "settings/imap"): 1,  # settings.getImap
    ("GET", "settings/pop"): 1,  # settings.getPop
    ("GET", "settings/vacation"): 1,  # settings.getVacation
    ("POST", "settings/sendAs"): 100,  # settings.sendAs.create
    ("DELETE", "settings/sendAs/{sendAsEmail}"): 5,  # settings.sendAs.delete
    ("GET", "settings/sendAs/{sendAsEmail}"): 1,  # settings.sendAs.get
    ("GET", "settings/sendAs"): 1,  # settings.sendAs.list
    ("PUT", "settings/sendAs/{sendAsEmail}"): 100,  # settings.sendAs.update
    ("POST", "settings/sendAs/{sendAsEmail}/verify"): 100,  # settings.sendAs.verify
    ("PUT", "settings/autoForwarding"): 5,  # settings.updateAutoForwarding
    ("PUT", "settings/imap"): 5,  # settings.updateImap
    ("PUT", "settings/pop"): 100,  # settings.updatePop
    ("PUT", "settings/vacation"): 5,  # settings.updateVacation
    ("POST", "stop"): 50,  # stop
    ("DELETE", "threads/{id}"): 20,  # threads.delete
    ("GET", "threads/{id}"): 40,  # threads.get
    ("GET", "threads"): 10,  # threads.list
    ("POST", "threads/{id}/modify"): 10,  # threads.modify
    ("POST", "threads/{id}/trash"): 20,  # threads.trash
    ("POST", "threads/{id}/untrash"): 10,  # threads.untrash
    ("POST", "watch"): 100,  # watch
    # Unpriced by Google; charged as their documented twin.
    ("PATCH", "labels/{id}"): 5,  # labels.patch (as labels.update)
    ("GET", "settings/language"): 1,  # settings.getLanguage (as settings.getImap)
    # settings.sendAs.patch (as settings.sendAs.update)
    ("PATCH", "settings/sendAs/{sendAsEmail}"): 100,
    ("PUT", "settings/language"): 5,  # settings.updateLanguage (as settings.updateImap)
}


_TABLE = PathTable(METHOD_COSTS)


def resolve_gmail(
    http_method: str,
    path: str,
    query: str = "",  # noqa: ARG001 - part of the resolver contract; Drive needs it
) -> tuple[str, int]:
    """Price a Gmail call in quota units.

    Everything draws on one bucket; only the cost varies. A path this table
    does not recognise is charged ``GMAIL_UNPRICED_COST``.
    """
    segments = path_segments(path, after="users")
    if segments is None:
        return "units", GMAIL_UNPRICED_COST
    cost = _TABLE.lookup(http_method, segments)
    return "units", GMAIL_UNPRICED_COST if cost is None else cost


#: Google Gmail API v1.
#:
#: The default is the per-user, per-minute quota, which is what a single script
#: runs into. The per-project ceiling is 1,200,000 units a minute; a job that
#: owns the whole project can say so with ``GMAIL.with_limits(units=1_200_000)``.
#:
#: Gmail publishes no limit on concurrent requests, so this profile models none.
GMAIL = ApiProfile(
    name="gmail",
    host="gmail.googleapis.com",
    limits={"units": 6_000},
    resolve=resolve_gmail,
)
