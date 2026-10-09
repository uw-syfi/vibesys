"""Tree differences, with rename detection, rendered as Git-style patches and name-status text.

Pure: callers pass the two snapshots and a reader that turns an entry into bytes.
Hunks come from ``difflib``, so a patch is valid and applies like Git's but is not
promised to choose the same hunks as Git's diff algorithm on ambiguous input.
Rename similarity is a line-based estimate of Git's byte-chunk score, with the
same 50% threshold.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from vs_project._git_objects import FileEntry, Snapshot

type Reader = Callable[[FileEntry], bytes]

_RENAME_THRESHOLD = 50
_RENAME_CANDIDATE_LIMIT = 2500
_CONTEXT_LINES = 3
_FUNCTION_LINE = re.compile(r"^[A-Za-z_$]")
_FUNCTION_TEXT_LIMIT = 80
_ABBREVIATION = 7
_FULL_ID = 40
_BINARY_PROBE = 8000
_FIRST_PRINTABLE = 0x20
_DELETE = 0x7F
_QUOTE_ESCAPES = {0x22: '\\"', 0x5C: "\\\\", 0x09: "\\t", 0x0A: "\\n", 0x0D: "\\r"}


@dataclass(frozen=True)
class Change:
    """One path that differs between two trees."""

    status: str
    """``A`` added, ``D`` deleted, ``M`` modified, ``T`` type changed, ``R`` renamed."""
    path: str
    """The path in the newer tree (the only path, except for a rename)."""
    before: FileEntry | None
    after: FileEntry | None
    old_path: str = ""
    """The path a rename came from; empty for every other status."""
    score: int = 100

    @property
    def source(self) -> str:
        """The path in the older tree."""
        return self.old_path or self.path


def detect_changes(
    before: Snapshot, after: Snapshot, read: Reader, *, renames: bool
) -> list[Change]:
    """The differences from ``before`` to ``after``, sorted by path, renames paired when asked."""
    changes: list[Change] = []
    for path in sorted(before.keys() & after.keys()):
        old, new = before[path], after[path]
        if old != new:
            kind = "T" if old.is_symlink() != new.is_symlink() else "M"
            changes.append(Change(kind, path, old, new))
    added = {path: after[path] for path in after.keys() - before.keys()}
    deleted = {path: before[path] for path in before.keys() - after.keys()}
    paired: list[Change] = []
    if renames and added and deleted:
        paired = _pair_renames(added, deleted, read)
        for change in paired:
            del added[change.path]
            del deleted[change.old_path]
    changes += paired
    changes += [Change("A", path, None, entry) for path, entry in added.items()]
    changes += [Change("D", path, entry, None) for path, entry in deleted.items()]
    return sorted(changes, key=lambda change: change.path)


def _pair_renames(
    added: dict[str, FileEntry], deleted: dict[str, FileEntry], read: Reader
) -> list[Change]:
    pairs: list[Change] = []
    sources = dict(deleted)
    remaining: list[str] = []
    for path in sorted(added):
        match = _exact_source(path, added[path], sources)
        if match is None:
            remaining.append(path)
            continue
        pairs.append(Change("R", path, sources.pop(match), added[path], old_path=match))
    if remaining and sources and len(remaining) * len(sources) <= _RENAME_CANDIDATE_LIMIT:
        pairs += _similar_renames(remaining, added, sources, read)
    return pairs


def _exact_source(path: str, entry: FileEntry, sources: dict[str, FileEntry]) -> str | None:
    candidates = sorted(source for source, old in sources.items() if old.blob == entry.blob)
    same_name = [
        source for source in candidates if source.rsplit("/", 1)[-1] == path.rsplit("/", 1)[-1]
    ]
    return (same_name or candidates or [None])[0]


def _similar_renames(
    targets: Sequence[str],
    added: dict[str, FileEntry],
    sources: dict[str, FileEntry],
    read: Reader,
) -> list[Change]:
    scored: list[tuple[int, str, str]] = []
    for target in targets:
        if added[target].is_symlink():
            continue
        new_data = read(added[target])
        for source, old in sources.items():
            if old.is_symlink():
                continue
            score = _similarity(read(old), new_data)
            if score >= _RENAME_THRESHOLD:
                scored.append((score, target, source))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_targets: set[str] = set()
    used_sources: set[str] = set()
    pairs = []
    for score, target, source in scored:
        if target in used_targets or source in used_sources:
            continue
        used_targets.add(target)
        used_sources.add(source)
        pairs.append(
            Change("R", target, sources[source], added[target], old_path=source, score=score)
        )
    return pairs


def _similarity(old: bytes, new: bytes) -> int:
    if not old and not new:
        return 100
    old_lines = _byte_lines(old)
    new_lines = _byte_lines(new)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    kept = sum(
        len(b"".join(old_lines[block.a : block.a + block.size]))
        for block in matcher.get_matching_blocks()
    )
    return min(100, kept * 100 // max(len(old), len(new), 1))


# -- rendering --------------------------------------------------------------------------


def render_name_status(changes: Iterable[Change]) -> str:
    """``git diff --name-status -z`` text: NUL-terminated status and path fields."""
    fields: list[str] = []
    for change in changes:
        if change.status == "R":
            fields += [f"R{change.score:03d}", change.old_path, change.path]
        else:
            fields += [change.status, change.path]
    return "".join(f"{field}\0" for field in fields)


def render_patch(changes: Iterable[Change], read: Reader, *, full_index: bool) -> str:
    """A ``git diff`` style patch for ``changes``."""
    width = _FULL_ID if full_index else _ABBREVIATION
    out: list[str] = []
    for change in changes:
        if change.status == "T":
            out.append(_render_one(Change("D", change.path, change.before, None), read, width))
            out.append(_render_one(Change("A", change.path, None, change.after), read, width))
        else:
            out.append(_render_one(change, read, width))
    return "".join(out)


def _quote(path: str) -> str:
    raw = path.encode()
    if all(_is_plain(byte) and byte not in _QUOTE_ESCAPES for byte in raw):
        return path
    body = "".join(
        _QUOTE_ESCAPES.get(byte, chr(byte) if _is_plain(byte) else f"\\{byte:03o}") for byte in raw
    )
    return f'"{body}"'


def _is_plain(byte: int) -> bool:
    return _FIRST_PRINTABLE <= byte < _DELETE


def _abbreviate(entry: FileEntry | None, width: int) -> str:
    return "0" * width if entry is None else entry.blob[:width]


def _header(change: Change, width: int) -> list[str]:
    lines = [f"diff --git {_quote('a/' + change.source)} {_quote('b/' + change.path)}\n"]
    before, after = change.before, change.after
    if before is None and after is not None:
        lines.append(f"new file mode {after.mode:o}\n")
    elif after is None and before is not None:
        lines.append(f"deleted file mode {before.mode:o}\n")
    elif before is not None and after is not None and before.mode != after.mode:
        lines += [f"old mode {before.mode:o}\n", f"new mode {after.mode:o}\n"]
    if change.status == "R":
        lines += [
            f"similarity index {change.score}%\n",
            f"rename from {change.source}\n",
            f"rename to {change.path}\n",
        ]
    if (before.blob if before else None) != (after.blob if after else None):
        suffix = f" {after.mode:o}" if before and after and before.mode == after.mode else ""
        lines.append(f"index {_abbreviate(before, width)}..{_abbreviate(after, width)}{suffix}\n")
    return lines


def _render_one(change: Change, read: Reader, width: int) -> str:
    lines = _header(change, width)
    old = read(change.before) if change.before else b""
    new = read(change.after) if change.after else b""
    if old == new:
        return "".join(lines)
    old_name = _quote("a/" + change.source) if change.before else "/dev/null"
    new_name = _quote("b/" + change.path) if change.after else "/dev/null"
    if b"\0" in old[:_BINARY_PROBE] or b"\0" in new[:_BINARY_PROBE]:
        lines.append(f"Binary files {old_name} and {new_name} differ\n")
        return "".join(lines)
    if not old and not new:
        return "".join(lines)
    tab = "\t" if " " in old_name + new_name else ""
    lines += [f"--- {old_name}{tab}\n", f"+++ {new_name}{tab}\n"]
    lines += _hunks(old.decode(errors="surrogateescape"), new.decode(errors="surrogateescape"))
    return "".join(lines)


def _lines(text: str) -> list[str]:
    """Split on newlines only, keeping them (``splitlines`` also splits on form feeds and more)."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _byte_lines(data: bytes) -> list[bytes]:
    parts = data.split(b"\n")
    lines = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _range(start: int, length: int) -> str:
    if length == 1:
        return f"{start + 1}"
    return f"{start if length == 0 else start + 1},{length}"


def _function_context(old_lines: Sequence[str], start: int) -> str:
    for index in range(start - 1, -1, -1):
        if _FUNCTION_LINE.match(old_lines[index]):
            return " " + old_lines[index].rstrip()[:_FUNCTION_TEXT_LIMIT]
    return ""


def _hunks(old: str, new: str) -> list[str]:
    old_lines = _lines(old)
    new_lines = _lines(new)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    out: list[str] = []
    for group in matcher.get_grouped_opcodes(_CONTEXT_LINES):
        first, last = group[0], group[-1]
        old_span = _range(first[1], last[2] - first[1])
        new_span = _range(first[3], last[4] - first[3])
        out.append(f"@@ -{old_span} +{new_span} @@{_function_context(old_lines, first[1])}\n")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out += _body(" ", old_lines[i1:i2])
                continue
            if tag in {"replace", "delete"}:
                out += _body("-", old_lines[i1:i2])
            if tag in {"replace", "insert"}:
                out += _body("+", new_lines[j1:j2])
    return out


def _body(prefix: str, lines: Sequence[str]) -> list[str]:
    out = []
    for line in lines:
        if line.endswith("\n"):
            out.append(f"{prefix}{line}")
        else:
            out += [f"{prefix}{line}\n", "\\ No newline at end of file\n"]
    return out
