"""Git diff adapter for PRs, merge groups, and pushes."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from .model import GIT, ROOT, _fail


def changed_paths(base: str, head: str, event: str, root: Path = ROOT) -> list[str]:
    """Return both names for renames/copies, including deleted paths."""
    if not base or not head:
        _fail("both --base and --head are required")
    zero_base = set(base) == {"0"}
    if zero_base and event != "push":
        _fail("zero base SHA is only valid for an initial push")
    if event == "pull_request":
        merge = subprocess.run(  # noqa: S603
            [GIT, "merge-base", base, head], cwd=root, capture_output=True, check=False, timeout=30
        )
        if merge.returncode:
            _fail(f"git merge-base failed: {merge.stderr.decode(errors='replace').strip()}")
        base = merge.stdout.decode().strip()
    elif event not in ("merge_group", "push"):
        _fail(f"unsupported event {event!r}")
    command = (
        [GIT, "diff-tree", "--root", "--no-commit-id", "--name-status", "-z", "-r", head]
        if zero_base
        else [GIT, "diff", "--name-status", "-z", "--find-renames", base, head]
    )
    result = subprocess.run(command, cwd=root, capture_output=True, check=False, timeout=30)  # noqa: S603
    if result.returncode:
        _fail(f"git diff failed: {result.stderr.decode(errors='replace').strip()}")
    parts = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(parts) - 1:
        status = parts[index]
        count = 2 if status.startswith(("R", "C")) else 1
        if status[:1] not in "ACDMRTUXB" or index + count >= len(parts):
            _fail(f"malformed git diff status {status!r}")
        paths.extend(parts[index + 1 : index + count + 1])
        index += count + 1
    return list(dict.fromkeys(paths))
