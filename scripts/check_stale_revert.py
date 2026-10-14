"""Fail a change that silently undoes recent commits by carrying stale copies of files.

A branch built from an old worktree can commit old copies of files it never meant to
touch. Its diff then reverts whatever landed on those files since, and a squash merge
lands the revert with a commit message about something else (#2343 undid seven merged
pull requests this way). The tests still pass, because the stale set is self-consistent.

The check compares each file the change touches with the file's history on the base
branch. A file whose new content is exactly what it was just before a recent base
commit changed it (a file that commit created counts as absent) undoes that commit. An
intended revert names the commit it reverts, the way ``git revert`` writes its message
("This reverts commit <sha>"), and is allowed.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from vs_project.api import run_git

RECENT_COMMITS = 200
"""How many first-parent base commits back a change is compared against."""
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")


@dataclass(frozen=True)
class StaleFile:
    """A file the change sets back to its content before ``undone`` changed it."""

    path: str
    undone: str
    subject: str


def _git(root: Path, *args: str) -> str:
    result = run_git(list(args), cwd=root, text=True)
    if result.returncode:
        message = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise RuntimeError(message)
    return result.stdout


def _ensure_base(root: Path, base_ref: str) -> None:
    """Fetch the base branch's history when the checkout is shallow or lacks it."""
    shallow = _git(root, "rev-parse", "--is-shallow-repository").strip() == "true"
    known = run_git(["rev-parse", "--verify", base_ref], cwd=root, text=True).returncode == 0
    if base_ref.startswith("origin/") and (shallow or not known):
        branch = base_ref.removeprefix("origin/")
        args = ["fetch", "--no-tags", *(["--unshallow"] if shallow else [])]
        _git(root, *args, "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")


_ABSENT = "0" * 40


def stale_files(root: Path, base_ref: str, head: str = "HEAD") -> list[StaleFile]:
    """Every file ``head`` sets back to its content before a recent ``base_ref`` commit."""
    base = _git(root, "merge-base", head, base_ref).strip()
    changed = _git(root, "diff", "--no-renames", "--name-only", base, head).splitlines()
    if not changed:
        return []
    reverted = _REVERTS.findall(_git(root, "log", "--format=%B", f"{base}..{head}"))
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
    commit = subject = ""
    for line in history.splitlines():
        if line.startswith("@"):
            commit, _, subject = line[1:].partition("\t")
        elif line.startswith(":") and not any(commit.startswith(sha) for sha in reverted):
            meta, _, path = line.partition("\t")
            old = meta.split()[2]
            if now.get(path, _ABSENT) != _ABSENT and path not in found and old == now[path]:
                found[path] = StaleFile(path, commit, subject)
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
