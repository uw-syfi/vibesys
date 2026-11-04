"""Fail when the repository tracks a tool's leftover file.

BSD and macOS ``sed -i -e EXPR FILE`` takes ``-e`` as the backup suffix and leaves a
``FILE-e`` copy beside each edited file; ``git add -A`` then commits it (one landed on
main in #1915). A failed ``patch`` or merge leaves ``.orig`` and ``.rej`` files the same
way. No tracked file in this repository legitimately ends in any of these.

Stdlib only, so CI's ``changes`` job runs it before any environment is installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

STRAY_SUFFIXES = ("-e", ".orig", ".rej")
"""Name endings that only tools' leftover copies have."""


def stray_files(paths: Iterable[str]) -> list[str]:
    """The paths whose file name ends in one of ``STRAY_SUFFIXES``."""
    return sorted(path for path in paths if Path(path).name.endswith(STRAY_SUFFIXES))


def main() -> int:
    """Report each tracked stray file and fail, or pass silently."""
    root = Path(__file__).resolve().parent.parent
    argv = ["git", "ls-files", "-z"]
    # lint-waiver: LW-235002 [S603]; a fixed git argv with no shell; stdlib so CI runs it before installing the environment.
    listed = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=True)  # noqa: S603
    found = stray_files(path for path in listed.stdout.split("\0") if path)
    for path in found:
        sys.stdout.write(f"{path}: a tool's leftover copy; `git rm` it\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
