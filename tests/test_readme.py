"""Checks that the README's examples are real code, not aspirations.

Documentation rots silently: a renamed argument leaves the prose looking fine
while every reader who copies it gets a TypeError. These tests make the README
fail the build instead.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

import googleapis_without_429 as package
from googleapis_without_429 import DRIVE, SHEETS

README = Path(__file__).resolve().parent.parent / "README.md"
CODE_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def python_blocks() -> list[str]:
    return CODE_BLOCK.findall(README.read_text(encoding="utf-8"))


def is_self_contained(block: str) -> bool:
    """A block that imports what it needs and needs no real credentials."""
    return "from googleapis_without_429 import" in block and "credentials" not in block


class TestExamples:
    def test_the_readme_actually_contains_examples(self) -> None:
        assert len(python_blocks()) >= 5

    @pytest.mark.parametrize(
        "block", python_blocks(), ids=lambda b: b.strip().splitlines()[0][:40]
    )
    def test_every_example_is_valid_python(self, block: str) -> None:
        compile(block, "<readme>", "exec")

    @pytest.mark.parametrize(
        "block",
        [b for b in python_blocks() if is_self_contained(b)],
        ids=lambda b: b.strip().splitlines()[0][:40],
    )
    def test_self_contained_examples_run(self, block: str) -> None:
        exec(compile(block, "<readme>", "exec"), {})  # noqa: S102

    def test_at_least_two_examples_are_executed(self) -> None:
        """A guard against the executable set quietly emptying out."""
        assert len([b for b in python_blocks() if is_self_contained(b)]) >= 2


def calls_in_readme() -> list[tuple[str, tuple[str, ...]]]:
    """Every call to a public name in the README, with its keyword arguments."""
    found = []
    for block in python_blocks():
        tree = ast.parse(block)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if not (hasattr(package, name) or name in {"with_limits", "bucket"}):
                continue
            keywords = tuple(kw.arg for kw in node.keywords if kw.arg)
            found.append((name, keywords))
    return found


def accepted_keywords(target: object) -> set[str]:
    """Keyword names a callable accepts, following inheritance through **kwargs.

    Checking one signature is not enough: RateLimitedSession forwards **kwargs
    to AuthorizedSession, so a lone signature accepts every name and a typo in
    the README passes unnoticed. Walking the MRO keeps genuine inherited
    arguments valid while a misspelling matches nothing anywhere.
    """
    passing = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    classes = inspect.getmro(target) if inspect.isclass(target) else [target]
    names: set[str] = set()
    for klass in classes:
        function = klass.__dict__.get("__init__") if inspect.isclass(klass) else klass
        if function is None:
            continue
        try:
            signature = inspect.signature(function)  # type: ignore[arg-type]
        except (TypeError, ValueError):  # pragma: no cover - C-level callables
            continue
        names.update(
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.kind in passing
        )
    return names


class TestNamesTheReaderWillTry:
    def test_the_readme_calls_the_public_api(self) -> None:
        assert len(calls_in_readme()) >= 4

    @pytest.mark.parametrize(
        ("name", "keywords"),
        calls_in_readme(),
        ids=[f"{n}({','.join(k)})" for n, k in calls_in_readme()],
    )
    def test_keyword_arguments_shown_in_the_readme_exist(
        self, name: str, keywords: tuple[str, ...]
    ) -> None:
        target = getattr(package, name, None)
        if target is None:  # a method such as with_limits, checked below
            return
        accepted = accepted_keywords(target)
        for keyword in keywords:
            assert keyword in accepted, (
                f"README calls {name}({keyword}=...) but it takes no such argument"
            )

    def test_with_limits_is_shown_with_real_bucket_names(self) -> None:
        """SHEETS.with_limits(read=...) and DRIVE.with_limits(units=...)."""
        shown = {
            keyword
            for name, keywords in calls_in_readme()
            if name == "with_limits"
            for keyword in keywords
        }
        assert shown <= set(SHEETS.limits) | set(DRIVE.limits), (
            f"README overrides limits that no shipped profile defines: {shown}"
        )

    def test_every_imported_name_is_actually_exported(self) -> None:
        imported: set[str] = set()
        for block in python_blocks():
            for line in block.splitlines():
                match = re.match(r"from googleapis_without_429 import (.+)", line)
                if match:
                    imported.update(part.strip() for part in match.group(1).split(","))

        assert imported
        missing = sorted(name for name in imported if not hasattr(package, name))
        assert not missing, (
            f"README imports names the package does not export: {missing}"
        )
