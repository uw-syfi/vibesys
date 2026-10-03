"""Which Git metadata a linked worktree needs to be readable inside a sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Entries of the shared repository directory that ``git status``, ``git diff``
# and ``git log`` read. Everything else is withheld: ``hooks`` (code Git would
# run), ``logs`` (reflogs of every branch), ``worktrees`` (the index and HEAD of
# every other worktree, including other roles' candidates), and any other entry.
_SHARED_ENTRIES = ("objects", "refs", "packed-refs", "HEAD", "config", "info", "shallow")


def linked_worktree_git_paths(workspace: Path) -> tuple[Path, ...]:
    """Return the out-of-tree Git paths a linked worktree needs, for read-only binding.

    A linked worktree's ``.git`` is a file naming its own gitdir
    (``<repo>/.git/worktrees/<name>``), whose ``commondir`` names the shared
    repository directory. Both live outside the workspace, so without them every
    ``git`` command in the sandbox fails with "not a git repository". The result
    is the worktree's own gitdir plus only the shared entries listed in
    ``_SHARED_ENTRIES`` that exist, so other worktrees' metadata, reflogs and
    hooks stay invisible.

    Limit: Git addresses objects by hash and refs by name inside one shared
    object store and ref namespace, so ``objects`` and ``refs`` still expose every
    commit and branch of the repository to read access. Hiding them would need a
    separate clone per sandbox, which is a different design. History stays
    immutable because every path is bound read-only.

    A workspace whose ``.git`` is a directory, or that has none, needs nothing
    extra and yields an empty tuple.
    """
    pointer = workspace / ".git"
    if not pointer.is_file():
        return ()
    prefix = "gitdir:"
    line = pointer.read_text(encoding="utf-8").strip()
    if not line.startswith(prefix):
        return ()
    gitdir = (workspace / line.removeprefix(prefix).strip()).resolve()
    if not gitdir.is_dir():
        return ()
    paths = [gitdir]
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        common = (gitdir / commondir_file.read_text(encoding="utf-8").strip()).resolve()
        if common.is_dir():
            paths.extend(common / name for name in _SHARED_ENTRIES if (common / name).exists())
    return tuple(path for path in paths if not path.is_relative_to(workspace))
