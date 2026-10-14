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

An intended undo declares the commit it undoes, by sha or by pull request number, on a
line of the change's message that says it reverts, relands or restores it: "This reverts
commit <sha>" (what ``git revert`` writes), "Reland #2071", "Restores #2163" (a file
moved back after a rename).

Stdlib only, so CI's ``changes`` job runs it before any environment is installed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RECENT_COMMITS = 200
"""How many first-parent base commits back a change is compared against."""
_DECLARATION = re.compile(r"\b(?:revert|reland|restore)(?:s|ed|ing)?\b(.*)", re.IGNORECASE)
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_PULL_REQUEST = re.compile(r"#(\d+)\b")


@dataclass(frozen=True)
class StaleFile:
    """A file the change sets back to its content before ``undone`` changed it."""

    path: str
    undone: str
    subject: str


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
            shas.update(_SHA.findall(match.group(1)))
            pull_requests.update(_PULL_REQUEST.findall(match.group(1)))
        return cls(frozenset(shas), frozenset(pull_requests))

    def covers(self, commit: str, subject: str) -> bool:
        return any(commit.startswith(sha) for sha in self.shas) or any(
            re.search(rf"#{number}\b", subject) for number in self.pull_requests
        )


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
        sys.stdout.write(
            f"{stale.path}: undoes {stale.undone[:12]} ({stale.subject}); "
            "rebuild the file from the base branch, or revert that commit with `git revert`\n"
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
