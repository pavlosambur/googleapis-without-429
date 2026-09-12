"""Matching a request path against a table of API method templates.

Google spells its paths with placeholders (``files/{fileId}/download``) and
occasional custom verbs (``keypairs/{keyPairId}:disable``). Both the Gmail and
Drive profiles need to recognise those, so the matcher lives here rather than
being written twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["PathTable", "path_segments"]


def path_segments(path: str, *, after: str) -> list[str] | None:
    """Split a URL path into segments, dropping everything up to a marker.

    Args:
        path: The URL path.
        after: A segment to skip past. Everything up to and including it is
            dropped, plus one more segment -- the identifier that follows it in
            Google's paths, as in ``users/{userId}``.

    Returns:
        The remaining segments, or ``None`` if the path does not have the
        expected shape.
    """
    segments = [segment for segment in path.split("/") if segment]
    try:
        index = segments.index(after)
    except ValueError:
        return None
    return segments[index + 2 :] or None


def _compile(template: str) -> tuple[str | None, ...]:
    """Turn a path template into per-segment matchers.

    ``None`` matches any single segment. A string starting with ``":"`` matches
    a segment ending in that custom verb. Anything else must match exactly.
    """
    matchers: list[str | None] = []
    for segment in template.strip("/").split("/"):
        if segment.startswith("{"):
            _, marker, verb = segment.partition("}:")
            matchers.append(":" + verb if marker else None)
        else:
            matchers.append(segment)
    return tuple(matchers)


def _segment_matches(matcher: str | None, segment: str) -> bool:
    if matcher is None:
        return True
    if matcher.startswith(":"):
        return segment.endswith(matcher)
    return segment == matcher


class PathTable:
    """Looks up a value by HTTP method and path, matching segment by segment.

    Entries are grouped by method and segment count, so a lookup compares a
    handful of candidates rather than the whole table, and within a group the
    most specific template wins: a literal segment beats a placeholder that
    would also match it, so a file whose id happened to be ``download`` is not
    priced as the download method.
    """

    def __init__(self, entries: Mapping[tuple[str, str], int]) -> None:
        self._by_shape: dict[tuple[str, int], list[tuple[tuple[str | None, ...], int]]]
        self._by_shape = {}
        for (verb, template), value in entries.items():
            matchers = _compile(template)
            shape = (verb.upper(), len(matchers))
            self._by_shape.setdefault(shape, []).append((matchers, value))

        for candidates in self._by_shape.values():
            candidates.sort(
                key=lambda entry: sum(m is not None for m in entry[0]),
                reverse=True,
            )

    def lookup(self, http_method: str, segments: Sequence[str]) -> int | None:
        """The value for this call, or ``None`` if the table has no entry."""
        shape = (http_method.upper(), len(segments))
        for matchers, value in self._by_shape.get(shape, ()):
            if all(
                _segment_matches(matcher, segment)
                for matcher, segment in zip(matchers, segments, strict=True)
            ):
                return value
        return None
