"""The files a Docker build context sends to the daemon, after ``.dockerignore``.

Follows Docker's rules (``github.com/moby/patternmatcher``): one pattern per
line, ``#`` comments, a leading ``!`` re-includes, the last matching pattern
wins, ``*`` and ``?`` stay within one path segment, ``**`` spans any number
of segments, and a pattern that matches a directory excludes everything under
it. The Dockerfile and ``.dockerignore`` are always part of the inputs: the
daemon reads both even when the patterns exclude them.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

DOCKERIGNORE = ".dockerignore"


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    exclusion: bool


def build_context_inputs(
    relative_paths: Iterable[str], *, dockerignore: str | None, dockerfile: str
) -> list[str]:
    """Return the context files a build reads, sorted.

    *relative_paths* are the regular files under the context, ``/``-separated
    and relative to it; *dockerignore* is the text of its ``.dockerignore``
    (``None``: there is none); *dockerfile* is the Dockerfile's relative path.
    Pure: the caller lists and reads the files.
    """
    patterns = _parse_patterns(dockerignore or "")
    always = {dockerfile, DOCKERIGNORE}
    return sorted(path for path in relative_paths if path in always or not _ignored(path, patterns))


def _parse_patterns(text: str) -> tuple[_Pattern, ...]:
    patterns = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exclusion = line.startswith("!")
        if exclusion:
            line = line[1:].strip()
        cleaned = posixpath.normpath(line.replace("\\", "/")).lstrip("/")
        if cleaned in {"", "."}:
            continue
        patterns.append(_Pattern(re.compile(_translate(cleaned)), exclusion=exclusion))
    return tuple(patterns)


def _translate(pattern: str) -> str:
    """Translate one cleaned pattern to a regex over ``/``-separated relative paths."""
    out = []
    segments = pattern.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            out.append(".*" if last else "(?:.*/)?")
            continue
        out.append(_translate_segment(segment))
        if not last:
            out.append("/")
    return "".join(out)


def _translate_segment(segment: str) -> str:
    out = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = segment.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = segment[index + 1 : end].replace("\\", "\\\\")
                if body.startswith(("!", "^")):
                    body = "^/" + body[1:]
                out.append("[" + body + "]" if body else re.escape("[]"))
                index = end
        elif char == "\\" and index + 1 < len(segment):
            index += 1
            out.append(re.escape(segment[index]))
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _ignored(relative: str, patterns: tuple[_Pattern, ...]) -> bool:
    """Whether the last pattern matching *relative* or one of its parents excludes it."""
    parts = relative.split("/")
    candidates = ["/".join(parts[: count + 1]) for count in range(len(parts))]
    ignored = False
    for pattern in patterns:
        if any(pattern.regex.fullmatch(candidate) for candidate in candidates):
            ignored = not pattern.exclusion
    return ignored
