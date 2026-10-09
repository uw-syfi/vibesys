"""Git ignore rules: parsing ``.gitignore``-style lines and deciding whether a path is ignored.

Pure: callers pass the lines and the layers; nothing here touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_project._git_pathspec import byte_view, wildmatch_regex

if TYPE_CHECKING:
    import re
    from collections.abc import Iterable, Sequence


@dataclass(frozen=True)
class IgnoreRule:
    """One ignore pattern."""

    regex: re.Pattern[str]
    negated: bool
    directories_only: bool
    anchored: bool
    """The pattern names a path from the rule file's directory; otherwise it names a basename."""

    def matches(self, relative: str, *, is_dir: bool) -> bool:
        """Whether the pattern selects ``relative`` (a path below the rule file's directory)."""
        if self.directories_only and not is_dir:
            return False
        subject = relative if self.anchored else relative.rsplit("/", 1)[-1]
        return self.regex.fullmatch(byte_view(subject)) is not None


def parse_ignore_lines(lines: Iterable[str]) -> tuple[IgnoreRule, ...]:
    """The rules of an ignore file's lines, in file order."""
    rules = []
    for raw in lines:
        line = _strip_trailing_spaces(raw.rstrip("\r\n"))
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        directories_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if not pattern:
            continue
        anchored = "/" in pattern
        pattern = pattern.removeprefix("/")
        rules.append(
            IgnoreRule(
                regex=wildmatch_regex(byte_view(pattern), pathname=True),
                negated=negated,
                directories_only=directories_only,
                anchored=anchored,
            )
        )
    return tuple(rules)


def _strip_trailing_spaces(line: str) -> str:
    """Drop trailing spaces unless escaped with a backslash."""
    end = len(line)
    while end > 0 and line[end - 1] == " " and not (end > 1 and line[end - 2] == "\\"):
        end -= 1
    return line[:end]


@dataclass(frozen=True)
class IgnoreLayer:
    """The rules of one ignore file and the directory (relative to the root) it applies below."""

    base: str
    rules: tuple[IgnoreRule, ...]


class IgnoreLayers:
    """Ignore layers from the highest precedence to the lowest."""

    def __init__(self, layers: Sequence[IgnoreLayer] = ()) -> None:
        """Hold ``layers``, highest precedence first."""
        self._layers = tuple(layers)

    def below(self, layer: IgnoreLayer) -> IgnoreLayers:
        """These layers with ``layer`` added at the highest precedence (a deeper ignore file)."""
        return IgnoreLayers((layer, *self._layers))

    def ignores(self, path: str, *, is_dir: bool) -> bool:
        """Whether the first layer with a matching rule ignores ``path`` (last match wins)."""
        for layer in self._layers:
            if layer.base:
                if not path.startswith(f"{layer.base}/"):
                    continue
                relative = path[len(layer.base) + 1 :]
            else:
                relative = path
            for rule in reversed(layer.rules):
                if rule.matches(relative, is_dir=is_dir):
                    return not rule.negated
        return False
