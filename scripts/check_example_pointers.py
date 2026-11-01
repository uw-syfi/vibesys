"""Refuse a change to an example repository's pinned commit unless the change declares it.

Each repository-native example is a submodule under ``examples/<family>/repositories/<name>``
(``docs/running-vibesys.md``). A worktree routinely shows those submodules modified, because
nobody checked out the pinned commit, and ``git add -A`` or ``git commit -a`` then records the
checked-out commit as the new pin (#3568 was ejected from the merge queue for it). A pin moves
only when someone means to bump the example, so a change that moves one must say so: a line of
a commit message in the change that reads ``Example-Bump: <path of the example repository>``,
once per pin it moves. A pin the message does not name fails the check; to unstage one that
was carried by accident, ``git restore --staged <path>`` and ``git submodule update`` it, or
``git checkout <base> -- <path>``.

A bump is explicit, so ``--github-output FILE`` also writes ``example_pointers=true`` (or
``false``) for CI, which then validates the examples in the commit stage, where
``validate-examples`` otherwise waits for the merge queue.

Stdlib only, so CI's ``changes`` job runs it before any environment is installed. Run it as
``python3 -m scripts.check_example_pointers`` from the repository root.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.check_stale_revert import GIT_COMMAND_TIMEOUT_SECONDS, bounded_git_argv

if TYPE_CHECKING:
    from collections.abc import Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

GITLINK_MODE = "160000"
"""Git's mode for a submodule entry."""
_POINTER_PARTS = 4
"""``examples/<family>/repositories/<name>``: the path shape of an example repository."""
_DECLARATION = re.compile(r"^Example-Bump:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


def is_example_repository(path: str) -> bool:
    """Whether ``path`` is ``examples/<family>/repositories/<name>``."""
    parts = path.split("/")
    return len(parts) == _POINTER_PARTS and parts[0] == "examples" and parts[2] == "repositories"


def changed_pointers(raw_diff: str) -> list[str]:
    """The example repositories a ``git diff --raw -z`` output adds, removes or moves."""
    fields = raw_diff.split("\0")
    paths: set[str] = set()
    for header, path in zip(fields[0::2], fields[1::2], strict=False):
        old_mode, new_mode = header.lstrip(":").split()[:2]
        if GITLINK_MODE in (old_mode, new_mode) and is_example_repository(path):
            paths.add(path)
    return sorted(paths)


def declared_bumps(message: str) -> set[str]:
    """The example repositories a commit message says it bumps, one ``Example-Bump:`` line each."""
    return set(_DECLARATION.findall(message))


def undeclared(changed: Iterable[str], message: str) -> list[str]:
    """The changed pins that no ``Example-Bump:`` line of ``message`` names."""
    declared = declared_bumps(message)
    return sorted(path for path in changed if path not in declared)


def _git(root: Path, *args: str) -> str:
    argv = bounded_git_argv(*args)
    # lint-waiver: LW-428117 [S603]; fixed git subcommands over refs and repo-relative paths, no shell; stdlib so CI runs it before installing the environment.
    result = subprocess.run(  # noqa: S603
        argv,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode:
        message = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise RuntimeError(message)
    return result.stdout


def pointer_changes(root: Path, base_ref: str, head: str = "HEAD") -> tuple[list[str], str]:
    """The pins ``head`` moves relative to ``base_ref``'s merge base, and the change's messages."""
    base = _git(root, "merge-base", head, base_ref).strip()
    raw = _git(
        root,
        "diff",
        "--raw",
        "-z",
        "--no-renames",
        "--ignore-submodules=none",
        base,
        head,
        "--",
        "examples",
    )
    return changed_pointers(raw), _git(root, "log", "--format=%B", f"{base}..{head}")


def main(argv: list[str] | None = None, *, root: Path = REPOSITORY_ROOT) -> int:
    """Report each undeclared pin and fail, or pass silently."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        changed, message = pointer_changes(root, args.base_ref, args.head)
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        sys.stderr.write(f"example-pointer check: {error}\n")
        return 2
    if args.github_output is not None:
        with args.github_output.open("a") as output:
            output.write(f"example_pointers={'true' if changed else 'false'}\n")
    missing = undeclared(changed, message)
    for path in missing:
        sys.stdout.write(
            f"{path}: this change moves the pinned commit of an example repository without "
            f"declaring it. If it is a bump, add a line `Example-Bump: {path}` to a commit "
            "message of the change. If not, a worktree's checked-out commit was staged by "
            f"accident: `git checkout origin/main -- {path}` puts the pin back\n"
        )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
