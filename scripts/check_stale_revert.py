"""Fail a change that silently undoes recent commits by carrying stale copies of files.

A branch built from an old worktree can commit old copies of files it never meant to
touch. Its diff then reverts whatever landed on those files since, and a squash merge
lands the revert with a commit message about something else (#2343 undid seven merged
pull requests this way). The tests still pass, because the stale set is self-consistent.

The check compares each file the change touches with the file's history on the base
branch. A file whose new content is exactly what it was just before a recent base
commit changed it undoes that commit. A deleted file undoes the commit that created it
when the change also sets another of that commit's files back: that is how #2343 dropped
the test file #2327 added. A deletion alone is not flagged, since deleting a file a
recent commit created is an ordinary refactor.

A stale copy that also carries one intended edit matches no old version of the file, so a
second pass works on lines. It blames the lines the change deletes against the last
``RECENT_COMMITS`` base commits (older lines are not blamed, which bounds the cost) and
flags a commit whose surviving lines in a file the change deletes nearly all of, when that
looks like an undo and not a rewrite: the change puts back the lines the commit removed,
or, for a commit that only added lines, adds next to nothing in their place. An ordinary
edit of a recently added line adds new lines and is not flagged.

An intended undo declares the commit it undoes, by sha or by pull request number, on a
line of the change's message that begins with one of these exact forms: "This reverts
commit <40-character sha>" (what ``git revert`` writes), "Reverts #2071" (what GitHub's
revert button writes, with an optional "owner/repo" before the "#"), "Reland #2071" or
"Restores #2163" (a file moved back after a rename).

Stdlib only, so CI's ``changes`` job runs it before any environment is installed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

RECENT_COMMITS = 200
"""How many first-parent base commits back a change is compared against."""
MAX_BLAMED_FILES = 60
"""How many changed files the line-level pass blames; a larger change is mostly moves and generated files."""
MIN_UNDONE_LINES = 5
"""A commit's lines must be deleted at least this many at a time to count as undone."""
MIN_UNDONE_SHARE = 0.9
"""And at least this share of the commit's lines that still stand in the file."""
MIN_LINE_LENGTH = 3
"""Stripped lines this short (braces, ``)``) say nothing about what a commit removed."""
MIN_PUT_BACK_LINES = 3
"""A commit that removed fewer distinct lines than this cannot be told apart by what it removed."""
MIN_PUT_BACK_SHARE = 0.8
"""Share of the distinct lines the commit removed that the change must add back."""
MIN_UNDONE_ADDITION = 10
"""Lines an added-only commit must have left in a file for deleting them to count as an undo."""
MAX_REPLACEMENT_LINES = 3
"""Lines the change may add in that file, an intended edit beside the stale copy."""

_DECLARATION = re.compile(
    r"^(?:This reverts commit (?P<sha>[0-9a-f]{40})\b"
    r"|(?:Reverts|Reland|Restores) (?:[\w.-]+/[\w.-]+)?#(?P<number>\d+)\b)",
    re.MULTILINE,
)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")


@dataclass(frozen=True)
class StaleFile:
    """A file the change sets back to its content before ``undone`` changed it."""

    path: str
    undone: str
    subject: str
    lines: int = 0
    """For a line-level finding, how many of ``undone``'s lines the change deletes; else 0."""


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = ["git", *args]
    # lint-waiver: LW-235001 [S603]; fixed git subcommands over refs and repo-relative paths, no shell; stdlib so CI runs it before installing the environment.
    return subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)  # noqa: S603


def _git(root: Path, *args: str) -> str:
    result = _run_git(root, *args)
    if result.returncode:
        message = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise RuntimeError(message)
    return result.stdout


def _ensure_base(root: Path, base_ref: str) -> None:
    """Fetch the base branch's history when the checkout is shallow or lacks it."""
    shallow = _git(root, "rev-parse", "--is-shallow-repository").strip() == "true"
    known = _run_git(root, "rev-parse", "--verify", base_ref).returncode == 0
    if base_ref.startswith("origin/") and (shallow or not known):
        branch = base_ref.removeprefix("origin/")
        args = ["fetch", "--no-tags", *(["--unshallow"] if shallow else [])]
        _git(root, *args, "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")


_ABSENT = "0" * 40


@dataclass(frozen=True)
class _Declared:
    """The commits a change's message says it reverts, relands or restores."""

    shas: frozenset[str]
    pull_requests: frozenset[str]

    @classmethod
    def from_message(cls, message: str) -> _Declared:
        shas: set[str] = set()
        pull_requests: set[str] = set()
        for match in _DECLARATION.finditer(message):
            if match.group("sha"):
                shas.add(match.group("sha"))
            else:
                pull_requests.add(match.group("number"))
        return cls(frozenset(shas), frozenset(pull_requests))

    def covers(self, commit: str, subject: str) -> bool:
        return commit in self.shas or any(
            re.search(rf"#{number}\b", subject) for number in self.pull_requests
        )


@dataclass(frozen=True)
class _Edit:
    """What the change does to one file: the base lines it deletes, the lines it adds."""

    deleted: frozenset[int]
    added: frozenset[str]
    added_count: int


def _edits(root: Path, base: str, head: str, changed: list[str]) -> dict[str, _Edit]:
    """The change's edit of each file that exists on both sides."""
    diff = _git(root, "diff", "-U0", "--no-renames", "--diff-filter=M", base, head, "--", *changed)
    deleted: dict[str, set[int]] = {}
    added: dict[str, list[str]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            path = line[len("--- a/") :]
            deleted.setdefault(path, set())
            added.setdefault(path, [])
        elif match := _HUNK.match(line):
            start, count = int(match.group(1)), int(match.group(2) or 1)
            deleted[path].update(range(start, start + count))
        elif line.startswith("+") and not line.startswith("+++ "):
            added[path].append(line[1:])
    return {
        path: _Edit(frozenset(deleted[path]), frozenset(x.strip() for x in added[path]), len(added[path]))
        for path in deleted
    }


def _blame(root: Path, base: str, oldest: str | None, path: str) -> dict[int, str]:
    """The recent commit that last wrote each line of ``path`` at ``base``; older lines are absent."""
    span = [f"{oldest}..{base}"] if oldest else [base]
    output = _git(root, "blame", "--porcelain", "--first-parent", *span, "--", path)
    owner: dict[int, str] = {}
    boundary: set[str] = set()
    current = ""
    for line in output.splitlines():
        if match := _BLAME_HEADER.match(line):
            current = match.group(1)
            owner[int(match.group(2))] = current
        elif line == "boundary":
            boundary.add(current)
    return {number: sha for number, sha in owner.items() if sha not in boundary}


def _removed_by(root: Path, commit: str, path: str) -> set[str]:
    """The distinct, non-trivial lines ``commit`` removed from ``path``,."""
    diff = _git(root, "show", "-U0", "--no-renames", "--format=", commit, "--", path)
    return {
        line[1:].strip()
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    }


def _looks_undone(root: Path, commit: str, path: str, edit: _Edit, removed: int) -> bool:
    """Whether deleting ``removed`` lines of ``commit`` from ``path`` reads as undoing it."""
    taken_out = _removed_by(root, commit, path)
    if not taken_out:
        return removed >= MIN_UNDONE_ADDITION and edit.added_count <= MAX_REPLACEMENT_LINES
    distinct = {line for line in taken_out if len(line) > MIN_LINE_LENGTH}
    return len(distinct) >= MIN_PUT_BACK_LINES and (
        len(distinct & edit.added) >= MIN_PUT_BACK_SHARE * len(distinct)
    )


def _undone_lines(
    root: Path, base: str, changed: list[str], head: str, declared: _Declared
) -> list[StaleFile]:
    """The recent undeclared commits whose lines the change deletes, file by file."""
    recent = _git(root, "rev-list", "--first-parent", f"-n{RECENT_COMMITS}", base).split()
    oldest = recent[-1] if len(recent) == RECENT_COMMITS else None
    found: list[StaleFile] = []
    for path, edit in sorted(_edits(root, base, head, changed).items())[:MAX_BLAMED_FILES]:
        if not edit.deleted:
            continue
        owner = _blame(root, base, oldest, path)
        standing = Counter(owner.values())
        deleted = Counter(owner[number] for number in edit.deleted if number in owner)
        for commit, count in deleted.items():
            subject = _git(root, "log", "-1", "--format=%s", commit).strip()
            if (
                count >= MIN_UNDONE_LINES
                and count >= MIN_UNDONE_SHARE * standing[commit]
                and not declared.covers(commit, subject)
                and _looks_undone(root, commit, path, edit, count)
            ):
                found.append(StaleFile(path, commit, subject, count))
                break
    return found


def stale_files(root: Path, base_ref: str, head: str = "HEAD") -> list[StaleFile]:
    """Every file ``head`` sets back to its content before a recent ``base_ref`` commit."""
    base = _git(root, "merge-base", head, base_ref).strip()
    changed = _git(root, "diff", "--no-renames", "--name-only", base, head).splitlines()
    if not changed:
        return []
    declared = _Declared.from_message(_git(root, "log", "--format=%B", f"{base}..{head}"))
    now = dict.fromkeys(changed, _ABSENT)
    for line in _git(root, "ls-tree", "--full-tree", head, "--", *changed).splitlines():
        meta, _, path = line.partition("\t")
        now[path] = meta.split()[2]
    history = _git(
        root,
        "log",
        "--first-parent",
        "--diff-merges=first-parent",
        "--no-renames",
        "--raw",
        "--no-abbrev",
        f"-n{RECENT_COMMITS}",
        "--format=@%H%x09%s",
        base,
        "--",
        *changed,
    )
    found: dict[str, StaleFile] = {}
    deleted: dict[str, StaleFile] = {}
    commit = subject = ""
    for line in history.splitlines():
        if line.startswith("@"):
            commit, _, subject = line[1:].partition("\t")
            continue
        if not line.startswith(":") or declared.covers(commit, subject):
            continue
        meta, _, path = line.partition("\t")
        old = meta.split()[2]
        if now[path] != _ABSENT and old == now[path] and path not in found:
            found[path] = StaleFile(path, commit, subject)
        elif now[path] == _ABSENT and old == _ABSENT and path not in deleted:
            deleted[path] = StaleFile(path, commit, subject)
    undone = {stale.undone for stale in found.values()}
    found.update({path: stale for path, stale in deleted.items() if stale.undone in undone})
    for stale in _undone_lines(root, base, changed, head, declared):
        found.setdefault(stale.path, stale)
    return sorted(found.values(), key=lambda stale: stale.path)


def main(argv: list[str] | None = None) -> int:
    """Report each stale file and fail, or pass silently."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    _ensure_base(root, args.base_ref)
    found = stale_files(root, args.base_ref, args.head)
    for stale in found:
        what = f"deletes {stale.lines} lines of" if stale.lines else "undoes"
        sys.stdout.write(
            f"{stale.path}: {what} {stale.undone[:12]} ({stale.subject}); "
            "rebuild the file from the base branch, or declare the undo "
            "(\"Reverts #N\", \"Reland #N\" or \"Restores #N\" on its own line of the message)\n"
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
