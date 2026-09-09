"""Fail if the source distribution carries files that should not ship.

Nothing in git protects against this. Internal notes are kept out of the
repository through .git/info/exclude, which build backends do not read, so a
file invisible to `git status` can still end up inside a published artefact.
That is a one-way mistake: an sdist on PyPI cannot be recalled or replaced.
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

# Paths that must never appear inside a distribution, relative to its root.
FORBIDDEN_PREFIXES = (".notes/", ".git/", ".venv/")

EXPECTED_TOP_LEVEL = {
    ".github",
    ".gitignore",
    ".pre-commit-config.yaml",
    "CHANGELOG.md",
    "LICENSE",
    "Makefile",
    "PKG-INFO",
    "README.md",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
}


def main() -> int:
    """Check the newest sdist in dist/ and return a process exit code."""
    archives = sorted(Path("dist").glob("*.tar.gz"))
    if not archives:
        print("no sdist in dist/ - run `uv build` first", file=sys.stderr)
        return 1

    archive = archives[-1]
    with tarfile.open(archive) as tar:
        members = [name.split("/", 1)[1] for name in tar.getnames() if "/" in name]

    forbidden = sorted(
        name
        for name in members
        if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
    )
    if forbidden:
        print(f"{archive.name} contains files that must not ship:", file=sys.stderr)
        for name in forbidden:
            print(f"  {name}", file=sys.stderr)
        return 1

    unexpected = sorted({name.split("/")[0] for name in members} - EXPECTED_TOP_LEVEL)
    if unexpected:
        print(f"{archive.name} has unexpected top-level entries:", file=sys.stderr)
        for name in unexpected:
            print(f"  {name}", file=sys.stderr)
        print(
            "Add it to EXPECTED_TOP_LEVEL if it belongs, or to the sdist "
            "include list in pyproject.toml if it does not.",
            file=sys.stderr,
        )
        return 1

    print(f"{archive.name}: {len(members)} files, nothing unexpected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
