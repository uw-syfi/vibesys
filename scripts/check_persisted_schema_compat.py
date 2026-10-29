"""Fail a change to a persisted shape that a document saved before it may not survive.

Each persisted shape (the run envelope, the runtime record, a strategy's checkpoint) is pinned
by a snapshot, a tracked ``*.persisted-schema.txt`` file that the shape's own test keeps equal
to the code. This check compares every snapshot of the base branch with the one at ``HEAD``.
Under an unchanged version it accepts only changes every saved document survives: new fields
with a default, closed sets and unions that gain members, looser bounds, renamed classes. Any
other change must raise the version; for the envelope, ``test_envelope_migrations.py`` then
requires the migration and a fixture it rewrites. A deleted snapshot, or a lowered version,
fails too. Each failure names the snapshot, the shape and the field's path.

In the merge queue the base is the queue's base, so two changes that each passed against an
older main are checked together before either lands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vs_core.api.testing import snapshot_problems
from vs_project.api import Git
from vs_sim.api import OsProcesses

if TYPE_CHECKING:
    from collections.abc import Mapping

SUFFIX = ".persisted-schema.txt"
"""The name every persisted-shape snapshot ends with; the check finds them by it."""

_GIT = Git(OsProcesses())


def problems(base: Mapping[str, str], head: Mapping[str, str]) -> list[str]:
    """What is wrong with each snapshot going from ``base`` to ``head`` (path to text)."""
    found: list[str] = []
    for path in sorted(set(base) | set(head)):
        found += snapshot_problems(base.get(path), head.get(path), path)
    return found


def _git(root: Path, *args: str) -> str:
    result = _GIT.run_text(list(args), cwd=root)
    result.check()
    return result.stdout


def _snapshots(root: Path, base_ref: str) -> tuple[dict[str, str], dict[str, str]]:
    """The snapshots at the merge base of ``HEAD`` and ``base_ref``, and in the working tree."""
    base = _git(root, "merge-base", "HEAD", base_ref).strip()
    listed = _git(root, "ls-tree", "-r", "--name-only", base).splitlines()
    before = {
        path: _git(root, "show", f"{base}:{path}") for path in listed if path.endswith(SUFFIX)
    }
    tracked = _git(root, "ls-files").splitlines()
    after = {
        path: (root / path).read_text(encoding="utf-8")
        for path in tracked
        if path.endswith(SUFFIX) and (root / path).is_file()
    }
    return before, after


def main(argv: list[str] | None = None) -> int:
    """Print each problem and fail, or pass silently."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    before, after = _snapshots(root, args.base_ref)
    found = problems(before, after)
    for problem in found:
        sys.stdout.write(f"{problem}\n")
    return int(bool(found))


if __name__ == "__main__":
    sys.exit(main())
