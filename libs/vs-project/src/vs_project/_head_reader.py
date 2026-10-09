"""Read a repository's ``HEAD`` commit from its files, without starting Git.

``GitTracker.current_sha`` is asked many times per run and each ``git rev-parse
HEAD`` costs a process spawn. The answer is a function of a few small files, so
this module reads them on every call. It keeps no state, hence it cannot go
stale and sees writes made by any process (the agent's own ``git commit``, a
crash-interrupted checkpoint).

It understands only the plain layout: a loose or packed branch ref, or a
detached ``HEAD``, in a main or linked worktree. Any other layout (reftable, symbolic-ref
chain, malformed file) is reported as ``Unreadable`` and the caller asks Git.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REF_PREFIX = "ref: "


@dataclass(frozen=True)
class Commit:
    """``HEAD`` resolves to ``sha``."""

    sha: str


@dataclass(frozen=True)
class Unborn:
    """``HEAD`` names a branch that has no commit yet."""


@dataclass(frozen=True)
class Unreadable:
    """The layout is not one this module understands; ask Git."""


HeadState = Commit | Unborn | Unreadable


def locate_git_dir(worktree: Path) -> Path | None:
    """Return the Git directory of a worktree whose top level is ``worktree``.

    ``None`` when ``worktree`` has no ``.git`` of its own (the caller asks Git).
    """
    dot_git = worktree / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        text = dot_git.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    prefix = "gitdir: "
    if not text.startswith(prefix):
        return None
    return (worktree / text.removeprefix(prefix)).resolve()


def read_head(git_dir: Path) -> HeadState:
    """Return the state of ``HEAD`` in ``git_dir``, or ``Unreadable`` when unsure."""
    try:
        common_dir = _common_dir(git_dir)
        if (common_dir / "reftable").exists() or (git_dir / "reftable").exists():
            return Unreadable()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if _OBJECT_ID.match(head):
            return Commit(head)
        if not head.startswith(_REF_PREFIX):
            return Unreadable()
        ref = head.removeprefix(_REF_PREFIX).strip()
        # Branches are shared by every linked worktree and live in the common
        # directory; other namespaces (per-worktree refs, tags) are left to Git.
        if not ref.startswith("refs/heads/") or ".." in ref.split("/"):
            return Unreadable()
        return _resolve_ref(common_dir, ref)
    except (OSError, UnicodeDecodeError):
        return Unreadable()


def _common_dir(git_dir: Path) -> Path:
    """Return the directory holding shared refs: ``git_dir`` itself unless a linked worktree."""
    try:
        pointer = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return git_dir
    return (git_dir / pointer).resolve()


def _resolve_ref(git_dir: Path, ref: str) -> HeadState:
    try:
        value = (git_dir / ref).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return _resolve_packed(git_dir, ref)
    except IsADirectoryError:
        return Unreadable()
    return Commit(value) if _OBJECT_ID.match(value) else Unreadable()


def _resolve_packed(git_dir: Path, ref: str) -> HeadState:
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
    except FileNotFoundError:
        return Unborn()
    for line in packed.splitlines():
        if line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name == ref:
            return Commit(sha) if _OBJECT_ID.match(sha) else Unreadable()
    return Unborn()
