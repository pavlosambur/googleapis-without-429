"""Check the shipped cost tables against Google's discovery documents.

The Gmail and Drive profiles price calls from a hand-assembled table: costs
come from a documentation page, paths from a discovery document. Neither
source announces a change, so an API gaining a method, renaming a path or
repricing a call leaves the table quietly wrong — over-counting at best,
sailing past a quota at worst.

This runs on a schedule rather than in the test suite, which is deliberately
offline. It reports:

- paths in a table that no longer exist upstream (a rename, or a typo)
- methods upstream that no table prices (these fall back to a default, which
  is safe but may be far from the real cost)

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

#: Whether the table is meant to cover the whole API. Gmail's prices every
#: method Google publishes a figure for, so anything missing is worth listing.
#: Drive's is a short list of exceptions to a path-shape fallback, so most
#: methods being absent is the design, not drift.
EXHAUSTIVE = {"gmail": True, "drive": False}

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
    """Report drift between one table and its discovery document."""
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

    stale = sorted(key for key in table if key not in normalised)
    if stale:
        print("  priced paths that no longer exist upstream:")
        for verb, path in stale:
            print(f"    {verb:<7} {path}")

    unpriced = sorted(
        (verb, path, method_id)
        for (verb, path), method_id in normalised.items()
        if (verb, path) not in table
    )
    if unpriced and EXHAUSTIVE[api]:
        print(
            f"  upstream methods with no entry ({len(unpriced)}, charged the fallback):"
        )
        for verb, path, method_id in unpriced:
            print(f"    {verb:<7} {method_id:<40} {path}")
    elif unpriced:
        print(f"  {len(unpriced)} methods use the path-shape fallback, as designed")

    return 1 if stale else 0


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
        print("Tables have drifted from upstream. Re-check the published costs.")
    else:
        print("No priced path has disappeared upstream.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
