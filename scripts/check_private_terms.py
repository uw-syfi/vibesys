"""Fail when tracked, staged or outgoing content names a term from a private list.

The list is a file of newline-separated, case-insensitive regular expressions kept
outside the repository (``$VIBESYS_PRIVATE_TERMS``, default
``~/.config/vibesys/private-terms.txt``): server names, hostnames and personal paths
that must not be committed. The terms are private by design, so the list cannot live in
the repository and CI cannot run this check; it runs on a developer machine, from the
opt-in pre-push hook. A missing list is not an error: the check says so and passes.

A finding is reported as ``path:line`` (or ``commit <sha>`` for a commit message) and never
repeats the matched text, so the report and the hook's output leak nothing.

Scanned: every tracked file as it is on disk, the lines the index adds, and the lines and
messages of commits that ``origin/main`` does not have yet.

Stdlib only, so it runs before any environment is installed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

DEFAULT_TERMS = Path("~/.config/vibesys/private-terms.txt")
TERMS_ENV = "VIBESYS_PRIVATE_TERMS"
OUTGOING_BASE = "origin/main"
EXIT_ERROR = 2
HUNK = re.compile(r"^@@ -\S+ \+(\d+)")


class Added(NamedTuple):
    """A line a diff adds: where it lands and what it says."""

    path: str
    line: int
    text: str


class BadTermError(ValueError):
    """The terms file holds a line that is not a regular expression."""

    def __init__(self, number: int) -> None:
        """Name the 1-based ``number`` of the offending line, never its text."""
        super().__init__(f"line {number} of the terms file is not a regular expression")


def parse_terms(text: str) -> list[re.Pattern[str]]:
    """Compile one case-insensitive pattern per non-blank, non-``#`` line.

    Raises ``BadTermError`` naming the line number, never the pattern.
    """
    patterns: list[re.Pattern[str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        term = raw.strip()
        if not term or term.startswith("#"):
            continue
        try:
            patterns.append(re.compile(term, re.IGNORECASE))
        except re.error as error:
            raise BadTermError(number) from error
    return patterns


def matching_lines(text: str, patterns: Iterable[re.Pattern[str]]) -> list[int]:
    """The 1-based numbers of the lines of ``text`` that any pattern matches."""
    compiled = list(patterns)
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if any(pattern.search(line) for pattern in compiled)
    ]


def added_lines(diff: str) -> Iterator[Added]:
    """The lines a ``git diff -U0`` or ``git log -p -U0`` output adds, with new-file numbers."""
    path: str | None = None
    line = 0
    in_header = False
    for row in diff.splitlines():
        if row.startswith("diff --git "):
            path, in_header = None, True
        elif in_header and row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else None
        elif hunk := HUNK.match(row):
            in_header, line = False, int(hunk.group(1))
        elif not in_header and path is not None and row.startswith("+"):
            yield Added(path, line, row[1:])
            line += 1


def findings(
    patterns: list[re.Pattern[str]],
    *,
    files: Iterable[tuple[str, str]],
    diffs: Iterable[str],
    messages: Iterable[tuple[str, str]],
) -> list[str]:
    """Where the patterns match in file texts ``(path, text)``, diffs and messages ``(sha, text)``.

    Each finding is a location only, sorted and de-duplicated; no finding contains matched text.
    """
    found: set[str] = set()
    for path, text in files:
        found.update(f"{path}:{number}" for number in matching_lines(text, patterns))
    for diff in diffs:
        found.update(
            f"{added.path}:{added.line}"
            for added in added_lines(diff)
            if any(pattern.search(added.text) for pattern in patterns)
        )
    found.update(
        f"commit {sha[:12]} message" for sha, text in messages if matching_lines(text, patterns)
    )
    return sorted(found)


def _run_git(root: Path, *args: str) -> tuple[int, str]:
    argv = ["git", *args]
    # lint-waiver: LW-246810 [S603]; a fixed git argv with no shell; stdlib so it runs before any environment is installed.
    # > A wrapper around subprocess adds nothing at this single boundary, and shell=True would
    # > weaken the argv guarantee; keeping one direct call is the simplest honest form.
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)  # noqa: S603
    return done.returncode, done.stdout


def _git(root: Path, *args: str) -> str:
    code, out = _run_git(root, *args)
    if code != 0:
        sys.stderr.write(f"private-terms check failed: git {args[0]} exited {code}\n")
        raise SystemExit(EXIT_ERROR)
    return out


def _tracked_files(root: Path) -> Iterator[tuple[str, str]]:
    for name in _git(root, "ls-files", "-z").split("\0"):
        path = root / name
        if not name or not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" not in data:
            yield name, data.decode("utf-8", errors="replace")


def _outgoing(root: Path) -> tuple[list[str], list[tuple[str, str]]]:
    if _run_git(root, "rev-parse", "--verify", "-q", OUTGOING_BASE)[0] != 0:
        return [], []
    span = f"{OUTGOING_BASE}..HEAD"
    diff = _git(root, "log", "-p", "-U0", "--no-color", "--no-ext-diff", "--format=", span)
    shas = _git(root, "rev-list", span).split()
    messages = [(sha, _git(root, "show", "-s", "--format=%B", sha)) for sha in shas]
    return [diff], messages


def main() -> int:
    """Scan the repository against the private list; 0 on a clean scan or a missing list."""
    root = Path(__file__).resolve().parent.parent
    terms_file = Path(os.environ.get(TERMS_ENV) or DEFAULT_TERMS).expanduser()
    if not terms_file.is_file():
        sys.stdout.write("private-terms check skipped: no terms file found\n")
        return 0
    try:
        patterns = parse_terms(terms_file.read_text(encoding="utf-8"))
    except BadTermError as error:
        sys.stderr.write(f"private-terms check failed: {error}\n")
        return EXIT_ERROR
    if not patterns:
        sys.stdout.write("private-terms check skipped: the terms file lists no term\n")
        return 0
    staged = _git(root, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff")
    outgoing_diffs, messages = _outgoing(root)
    found = findings(
        patterns,
        files=_tracked_files(root),
        diffs=[staged, *outgoing_diffs],
        messages=messages,
    )
    for location in found:
        sys.stdout.write(f"{location}: names a private term; replace it with a generic value\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
