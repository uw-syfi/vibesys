"""Git's wildcard matching and the pathspec subset of ``GitRepository``.

``wildmatch_regex`` translates Git's wildmatch patterns (used by pathspecs and
ignore files). ``Pathspecs`` parses the closed pathspec subset documented in
``vs_project.api.git_repository`` (a relative path, ``:(literal)``, ``:(glob)``,
``:(exclude)``) and answers which repository paths it selects.

Pure: no filesystem, no repository.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_WILDCARDS = frozenset("*?[\\")

_POSIX_CLASSES: dict[str, str] = {
    "alpha": "a-zA-Z",
    "digit": "0-9",
    "alnum": "a-zA-Z0-9",
    "upper": "A-Z",
    "lower": "a-z",
    "space": " \\t\\n\\r\\f\\v",
    "blank": " \\t",
    "xdigit": "0-9a-fA-F",
    "punct": re.escape(string.punctuation),
    "cntrl": "\\x00-\\x1f\\x7f",
    "graph": "!-~",
    "print": " -~",
}


class PathspecError(ValueError):
    """A pathspec outside the supported subset or outside the repository."""


def _bracket(pattern: str, start: int, *, pathname: bool) -> tuple[str, int] | None:
    """Translate the bracket expression opening at ``start``; ``None`` when unterminated."""
    index = start + 1
    negated = index < len(pattern) and pattern[index] in "!^"
    if negated:
        index += 1
    members: list[str] = []
    first = True
    while index < len(pattern):
        char = pattern[index]
        if char == "]" and not first:
            body = "".join(members)
            if pathname:
                regex = f"[^{body}/]" if negated else f"(?!/)[{body}]"
            else:
                regex = f"[^{body}]" if negated else f"[{body}]"
            return regex, index + 1
        first = False
        if pattern.startswith("[:", index) and (end := pattern.find(":]", index + 2)) != -1:
            name = pattern[index + 2 : end]
            if name not in _POSIX_CLASSES:
                return None
            members.append(_POSIX_CLASSES[name])
            index = end + 2
        elif char == "\\" and index + 1 < len(pattern):
            members.append(re.escape(pattern[index + 1]))
            index += 2
        elif (
            char != "-"
            and pattern[index + 1 : index + 2] == "-"
            and index + 2 < len(pattern)
            and pattern[index + 2] != "]"
        ):
            members.append(f"{re.escape(char)}-{re.escape(pattern[index + 2])}")
            index += 3
        else:
            members.append(re.escape(char))
            index += 1
    return None


def _stars(pattern: str, start: int, *, pathname: bool) -> tuple[str, int]:
    """Translate the run of ``*`` at ``start``; a lone ``**`` path component spans directories."""
    end = start
    while end < len(pattern) and pattern[end] == "*":
        end += 1
    whole_component = (
        pathname
        and end - start >= 2  # noqa: PLR2004  # lint-waiver: LW-415560 [PLR2004]; two stars is the wildmatch syntax for a directory-spanning component.
        and (start == 0 or pattern[start - 1] == "/")
        and (end == len(pattern) or pattern[end] == "/")
    )
    if whole_component:
        if end == len(pattern):
            return ".*", end
        return "(?:.*/)?", end + 1
    return ("[^/]*" if pathname else ".*"), end


def byte_view(text: str) -> str:
    """``text`` as Git sees it: one character per UTF-8 byte, so ``?`` matches a byte, not a letter."""
    return text.encode("utf-8", "surrogateescape").decode("latin-1")


@cache
def wildmatch_regex(pattern: str, *, pathname: bool) -> re.Pattern[str]:
    """The regular expression matching ``pattern`` against a whole string.

    With ``pathname``, ``*`` and ``?`` do not cross ``/`` and ``**`` does, as in
    ``:(glob)`` pathspecs and ignore files. Without it, they cross ``/``.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            piece, index = _stars(pattern, index, pathname=pathname)
            out.append(piece)
        elif char == "?":
            out.append("[^/]" if pathname else ".")
            index += 1
        elif char == "[" and (bracket := _bracket(pattern, index, pathname=pathname)):
            piece, index = bracket
            out.append(piece)
        elif char == "\\" and index + 1 < len(pattern):
            out.append(re.escape(pattern[index + 1]))
            index += 2
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("".join(out), re.DOTALL)


@dataclass(frozen=True)
class _Item:
    """One pathspec: whether it subtracts, what it selects, and the fixed text it begins with."""

    exclude: bool
    text: str
    """The path or pattern, normalized (no leading ``./``, no trailing ``/``; empty selects all)."""
    test: Callable[[str], bool]
    wild: bool
    """Whether ``text`` holds wildcards, so any path below any directory may match."""


def _literal(text: str) -> Callable[[str], bool]:
    if not text:
        return lambda _path: True
    prefix = f"{text}/"
    return lambda path: path == text or path.startswith(prefix)


def _parse_magic(spec: str) -> tuple[set[str], str]:
    """Split ``spec`` into its magic words and the remaining path or pattern."""
    if not spec.startswith(":"):
        return set(), spec
    if spec.startswith(":("):
        close = spec.find(")")
        if close == -1:
            message = f"unterminated pathspec magic: {spec!r}"
            raise PathspecError(message)
        words = {word.strip() for word in spec[2:close].split(",") if word.strip()}
        unsupported = words - {"literal", "glob", "exclude", "top"}
        if unsupported:
            message = f"unsupported pathspec magic {sorted(unsupported)}: {spec!r}"
            raise PathspecError(message)
        return words, spec[close + 1 :]
    if spec[1:2] in ("!", "^"):
        return {"exclude"}, spec[2:]
    message = f"unsupported pathspec magic: {spec!r}"
    raise PathspecError(message)


def _normalize(text: str, spec: str) -> str:
    """Resolve ``.`` and ``..`` lexically, as Git does; reject a path that leaves the repository."""
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                message = f"pathspec is outside the repository: {spec!r}"
                raise PathspecError(message)
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _item(spec: str) -> _Item:
    words, text = _parse_magic(spec)
    if "literal" in words and "glob" in words:
        message = f"pathspec magic literal and glob conflict: {spec!r}"
        raise PathspecError(message)
    normalized = _normalize(text, spec)
    exclude = "exclude" in words
    if "literal" in words or not (_WILDCARDS & set(normalized)):
        return _Item(exclude, normalized, _literal(normalized), wild=False)
    regex = wildmatch_regex(byte_view(normalized), pathname="glob" in words)
    return _Item(
        exclude, normalized, lambda path: regex.fullmatch(byte_view(path)) is not None, wild=True
    )


class Pathspecs:
    """A parsed pathspec list: includes, minus excludes. No includes selects everything."""

    def __init__(self, specs: Sequence[str]) -> None:
        """Parse ``specs``; raises ``PathspecError`` for anything outside the subset."""
        self._items = tuple((spec, _item(spec)) for spec in specs)
        self._includes = tuple(item for _, item in self._items if not item.exclude)
        self._excludes = tuple(item for _, item in self._items if item.exclude)

    def matches(self, path: str) -> bool:
        """Whether ``path`` (relative, POSIX) is selected."""
        included = not self._includes or any(item.test(path) for item in self._includes)
        return included and not any(item.test(path) for item in self._excludes)

    def unmatched(self, paths: Sequence[str]) -> list[str]:
        """The include pathspecs (as written) that select none of ``paths``."""
        return [
            spec
            for spec, item in self._items
            if not item.exclude and item.text and not any(item.test(path) for path in paths)
        ]

    def may_match_below(self, directory: str) -> bool:
        """Whether some path inside ``directory`` could be selected (false only when sure)."""
        if not self._includes:
            return True
        return any(
            item.wild
            or not item.text
            or item.text == directory
            or item.text.startswith(f"{directory}/")
            or directory.startswith(f"{item.text}/")
            for item in self._includes
        )

    def names_exactly(self, path: str) -> bool:
        """Whether an include pathspec spells ``path`` or a path below it, without wildcards."""
        return any(
            not item.wild and (item.text == path or item.text.startswith(f"{path}/"))
            for item in self._includes
        )
