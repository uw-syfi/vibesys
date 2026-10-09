"""Match paths against the pathspec subset of the ``GitRepository`` contract.

A library-backed implementation has to answer "does this path fall under these
pathspecs" itself, because libgit2 does not implement Git's pathspec magic. This
module is that answer for the closed subset documented in
``vs_project.api.git_repository``:

* a relative path: the path itself or anything below it; ``.`` is the root;
* ``:(literal)<path>``: the same, with no character treated as a wildcard;
* ``:(glob)<wildmatch>``: ``*`` and ``?`` stay inside one path component, and
  ``**`` crosses components when it is a whole component (``**/x``, ``x/**``,
  ``a/**/b``);
* ``:(exclude)<path-or-wildcard>``: subtracts from the other pathspecs; without
  ``glob`` a ``*`` or ``?`` also crosses ``/`` (Git's default matching).

``compile_pathspecs`` returns ``None`` for anything outside that subset (other
magic, bracket expressions, backslash escapes, ``..`` or absolute paths), so
the caller can hand the whole request to the Git CLI instead of guessing.
Matching is pure and keeps no state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_WILDCARDS = frozenset("*?[\\")
_SUPPORTED_WILDCARDS = frozenset("*?")
_CROSSING_STARS = 2
"""``**`` is the wildmatch syntax for "crosses directories"."""
_MAGIC = re.compile(r"^:\((literal|glob|exclude)\)(.*)$", re.DOTALL)


@dataclass(frozen=True)
class _Item:
    """One pathspec: a directory-or-file prefix, or a wildcard pattern."""

    prefix: str | None
    pattern: re.Pattern[bytes] | None

    def matches(self, path: str) -> bool:
        if self.pattern is not None:
            return self.pattern.fullmatch(path.encode(errors="surrogateescape")) is not None
        prefix = self.prefix or ""
        return not prefix or path == prefix or path.startswith(f"{prefix}/")


@dataclass(frozen=True)
class PathspecMatcher:
    """Whether a path is selected by a list of pathspecs."""

    includes: tuple[_Item, ...]
    excludes: tuple[_Item, ...]

    def matches(self, path: str) -> bool:
        """Whether ``path`` is selected: no includes means everything, then excludes subtract."""
        if self.includes and not any(item.matches(path) for item in self.includes):
            return False
        return not any(item.matches(path) for item in self.excludes)


def compile_pathspecs(specs: Sequence[str]) -> PathspecMatcher | None:
    """Compile ``specs``; ``None`` when any of them is outside the supported subset."""
    includes: list[_Item] = []
    excludes: list[_Item] = []
    for spec in specs:
        parsed = _parse(spec)
        if parsed is None:
            return None
        item, is_exclude = parsed
        (excludes if is_exclude else includes).append(item)
    return PathspecMatcher(tuple(includes), tuple(excludes))


def _parse(spec: str) -> tuple[_Item, bool] | None:
    magic = _MAGIC.match(spec)
    if magic is None:
        return None if spec.startswith(":") else _parse_body("plain", spec)
    return _parse_body(magic.group(1), magic.group(2))


def _parse_body(kind: str, body: str) -> tuple[_Item, bool] | None:
    unsupported = {
        "plain": _WILDCARDS,
        "literal": frozenset[str](),
    }.get(kind, _WILDCARDS - _SUPPORTED_WILDCARDS)
    normalized = None if unsupported & set(body) else _normalize(body)
    if normalized is None:
        return None
    is_exclude = kind == "exclude"
    if kind in {"plain", "literal"} or not _SUPPORTED_WILDCARDS & set(normalized):
        return _Item(normalized, None), is_exclude
    return _Item(None, _wildcard(normalized, pathname=kind == "glob")), is_exclude


def _normalize(body: str) -> str | None:
    """A relative path without ``.``/``..`` components, or ``None`` when it is not one."""
    if body.startswith("/") or "\0" in body:
        return None
    parts = [part for part in body.split("/") if part not in {"", "."}]
    if ".." in parts:
        return None
    return "/".join(parts)


def _wildcard(pattern: str, *, pathname: bool) -> re.Pattern[bytes]:
    """Translate a ``*``/``?`` pattern the way Git's wildmatch reads it.

    Like Git, it matches bytes: ``?`` is one byte, not one character.
    """
    out: list[bytes] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            end = index
            while end < len(pattern) and pattern[end] == "*":
                end += 1
            whole_component = (
                pathname
                and end - index >= _CROSSING_STARS
                and (index == 0 or pattern[index - 1] == "/")
                and (end == len(pattern) or pattern[end] == "/")
            )
            if whole_component and end < len(pattern):
                out.append(b"(?:.*/)?")
                index = end + 1
                continue
            out.append(b".*" if whole_component or not pathname else b"[^/]*")
            index = end
            continue
        out.append(
            (b"[^/]" if pathname else b".")
            if char == "?"
            else re.escape(char.encode(errors="surrogateescape"))
        )
        index += 1
    return re.compile(b"".join(out), re.DOTALL)
