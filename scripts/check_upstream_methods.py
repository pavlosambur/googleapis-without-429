"""Check the shipped cost tables against Google's discovery documents.

The Gmail and Drive profiles price calls from a hand-assembled table: costs
come from a documentation page, paths from a discovery document. Neither
source announces a change, so an API gaining a method, renaming a path or
repricing a call leaves the table quietly wrong — over-counting at best,
sailing past a quota at worst.

This runs on a schedule rather than in the test suite, which is deliberately
offline. It fails -- rather than merely printing -- when any of these change:

- a priced path no longer exists upstream (a rename, or a typo in a table)
- the number of methods in a discovery document differs from the recorded one
- for Gmail, the set of methods with no published price differs from the
  recorded one, in either direction

Everything it checks is pinned to a recorded baseline, because a scheduled run
that prints a warning and exits zero is a run nobody reads.

It cannot detect a *reprice*: discovery carries no costs. The dates below are
the honest answer to "when was this last checked against the page".

    uv run python scripts/check_upstream_methods.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

from googleapis_without_429.profiles.drive import EXPLICIT_COSTS
from googleapis_without_429.profiles.gmail import METHOD_COSTS

DISCOVERY = {
    "gmail": "https://gmail.googleapis.com/$discovery/rest?version=v1",
    "drive": "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest",
}

#: Prefix stripped from a discovery path before comparing it with a table.
PATH_PREFIX = {
    "gmail": "gmail/v1/users/{userId}/",
    "drive": "",
}

#: How many methods each discovery document held when the tables were last
#: checked against Google's published costs, on 2026-09-12.
#:
#: A count catches a method arriving or disappearing. It cannot catch one
#: being added and another removed between two runs; the stale-path check
#: covers that for anything actually priced.
EXPECTED_METHOD_COUNT = {"gmail": 79, "drive": 64}

#: Gmail methods Google publishes no price for. They are charged the profile's
#: fallback, which is the highest documented cost -- safe, but far from the
#: real figure for most of them.
#:
#: Recorded so that a change fails the run instead of scrolling past in a green
#: log: a new unpriced method arriving, or one of these finally gaining a
#: published price, both mean the table needs revisiting.
#:
#: Drive has no entry here on purpose. Its table is a short list of exceptions
#: to a path-shape fallback, so most methods being absent is the design.
UNPRICED_BASELINE: dict[str, frozenset[tuple[str, str]]] = {
    "gmail": frozenset(
        {
            ("DELETE", "settings/cse/identities/{cseEmailAddress}"),
            ("DELETE", "settings/sendAs/{sendAsEmail}/smimeInfo/{id}"),
            ("GET", "settings/cse/identities"),
            ("GET", "settings/cse/identities/{cseEmailAddress}"),
            ("GET", "settings/cse/keypairs"),
            ("GET", "settings/cse/keypairs/{keyPairId}"),
            ("GET", "settings/sendAs/{sendAsEmail}/smimeInfo"),
            ("GET", "settings/sendAs/{sendAsEmail}/smimeInfo/{id}"),
            ("PATCH", "settings/cse/identities/{emailAddress}"),
            ("POST", "settings/cse/identities"),
            ("POST", "settings/cse/keypairs"),
            ("POST", "settings/cse/keypairs/{keyPairId}:disable"),
            ("POST", "settings/cse/keypairs/{keyPairId}:enable"),
            ("POST", "settings/cse/keypairs/{keyPairId}:obliterate"),
            ("POST", "settings/sendAs/{sendAsEmail}/smimeInfo"),
            ("POST", "settings/sendAs/{sendAsEmail}/smimeInfo/{id}/setDefault"),
        }
    ),
}

TIMEOUT_SECONDS = 30


def fetch(url: str) -> dict[str, Any]:
    """Download and parse one discovery document."""
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        payload: dict[str, Any] = json.load(response)
    return payload


def methods_in(document: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Every ``(HTTP method, path)`` in a discovery document, to its method id."""
    found: dict[tuple[str, str], str] = {}

    def walk(resource: dict[str, Any], prefix: str = "") -> None:
        for name, body in resource.get("resources", {}).items():
            for method_name, method in body.get("methods", {}).items():
                key = (method["httpMethod"], method["path"])
                found[key] = f"{prefix}{name}.{method_name}"
            walk(body, f"{prefix}{name}.")

    walk(document)
    return found


def compare(api: str, table: dict[tuple[str, str], int]) -> int:
    """Report drift between one table and its discovery document.

    Returns the number of differences found, each of which fails the run.
    """
    document = fetch(DISCOVERY[api])
    upstream = methods_in(document)
    prefix = PATH_PREFIX[api]

    normalised = {
        (verb, path[len(prefix) :] if path.startswith(prefix) else path): method_id
        for (verb, path), method_id in upstream.items()
    }

    print(
        f"\n{api}: revision {document.get('revision')}, "
        f"{len(upstream)} methods upstream, {len(table)} priced"
    )

    problems = 0

    stale = sorted(key for key in table if key not in normalised)
    if stale:
        problems += 1
        print("  priced paths that no longer exist upstream:")
        for verb, path in stale:
            print(f"    {verb:<7} {path}")

    expected_count = EXPECTED_METHOD_COUNT[api]
    if len(upstream) != expected_count:
        problems += 1
        print(
            f"  method count changed: {expected_count} when last checked, "
            f"{len(upstream)} now"
        )

    unpriced = {key for key in normalised if key not in table}
    baseline = UNPRICED_BASELINE.get(api)
    if baseline is None:
        print(f"  {len(unpriced)} methods use the path-shape fallback, as designed")
        return problems

    for label, difference in (
        ("upstream methods with no entry, not seen before", unpriced - baseline),
        ("methods that were unpriced and are no longer", baseline - unpriced),
    ):
        if not difference:
            continue
        problems += 1
        print(f"  {label} ({len(difference)}):")
        for verb, path in sorted(difference):
            print(f"    {verb:<7} {normalised.get((verb, path), '-'):<40} {path}")

    if not (unpriced - baseline) and not (baseline - unpriced):
        print(f"  {len(unpriced)} methods unpriced, exactly as recorded")

    return problems


def main() -> int:
    """Compare every shipped table with its discovery document."""
    failures = 0
    for api, table in (("gmail", METHOD_COSTS), ("drive", EXPLICIT_COSTS)):
        try:
            failures += compare(api, table)
        except OSError as error:
            print(f"{api}: could not fetch discovery: {error}", file=sys.stderr)
            failures += 1

    print()
    if failures:
        print(
            f"{failures} difference(s) from the recorded baseline. Re-check the "
            "published costs, then update the baseline in this file."
        )
    else:
        print("Both tables match the recorded baseline.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
