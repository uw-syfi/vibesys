"""Append patterns to a repository-local ``info/exclude`` file.

Both ``GitRepository`` implementations edit this plain text file directly (no
Git command is involved), so the rule lives here once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def append_excludes(exclude_file: Path, patterns: Sequence[str]) -> tuple[str, ...]:
    """Add the ``patterns`` not already present, once each; return them in the order given."""
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_file.read_text() if exclude_file.exists() else ""
    have = set(existing.splitlines())
    new = tuple(pattern for pattern in dict.fromkeys(patterns) if pattern not in have)
    if not new:
        return ()
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    exclude_file.write_text(existing + prefix + "\n".join(new) + "\n")
    return new
