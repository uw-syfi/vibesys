"""Refuse host Git on a work tree that holds a nested repository.

Git checks a gitlink (the entry a nested repository or submodule is recorded as) by
running a child Git inside it, which reads that repository's own config and
attributes. In a workspace the agent writes both, so a filter driver it defines would
run on the host, unconfined, at the next ``git add``, ``git status`` or ``git diff``;
no fixed config key can turn off a driver whose name the repository chooses. Host Git
therefore never reads a work tree with a gitlink in the index it reads, or with a
``.git`` where the command looks: below the top level, inside the directories its
pathspecs select, or in a directory above them.

Linked worktrees of the same repository are the exception: Git registered them in the
host-owned repository directory, and each is a work tree of its own, guarded by the
commands that read it.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_project._git_pathspec import PathspecError, Pathspecs
from vs_project._workspace_tree import (
    TreeLimitError,
    WorkspacePathError,
    WorkspaceTree,
    open_workspace_tree,
)
from vs_project.api.git_repository import NestedRepositoryError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

GITLINK_MODE = "160000"
"""The index mode Git records a nested repository with."""

SCAN_LIMIT = 2_000_000
"""Entries the scan looks at before it refuses the tree instead (about a second's walk)."""

_GIT = ".git"


def require_no_nested_repository(
    work_tree: Path,
    pathspecs: Sequence[str] = (),
    *,
    gitlinks: Iterable[str] = (),
    linked_worktrees: Iterable[Path] = (),
) -> None:
    """Raise ``NestedRepositoryError`` for ``gitlinks`` or a ``.git`` Git would look at.

    ``pathspecs`` are those of the command about to run (none selects the whole tree).
    The top level's own ``.git`` is the repository host Git was pinned to and is allowed,
    and so is the ``.git`` of each of ``linked_worktrees``, whose contents are not
    scanned. The walk goes by descriptor and follows no symlink (see ``WorkspaceTree``).
    """
    try:
        roots = Pathspecs(pathspecs).fixed_prefixes()
    except PathspecError:
        roots = ("",)
    top = work_tree.resolve()
    linked = {
        path.resolve().relative_to(top).as_posix()
        for path in linked_worktrees
        if path.resolve().is_relative_to(top) and path.resolve() != top
    }
    found: set[str] = set(gitlinks)
    try:
        with open_workspace_tree(work_tree) as tree:
            for root in roots:
                found.update(tree.entries_named(_GIT, limit=SCAN_LIMIT, below=root, skip=linked))
                found.update(_above(tree, root))
    except TreeLimitError as error:
        raise NestedRepositoryError((), scan_limit=SCAN_LIMIT) from error
    allowed = {_GIT, *(f"{path}/{_GIT}" for path in linked)}
    if nested := sorted(found - allowed):
        raise NestedRepositoryError(nested)


def _above(tree: WorkspaceTree, root: str) -> list[str]:
    """The ``.git`` entries in the directories above ``root``, below the top level."""
    found = []
    for ancestor in PurePosixPath(root).parents:
        if not ancestor.parts:
            continue
        candidate = (ancestor / _GIT).as_posix()
        try:
            present = tree.kind(candidate) is not None
        except WorkspacePathError:
            present = False  # a symlink on the way: Git does not follow it either
        if present:
            found.append(candidate)
    return found
