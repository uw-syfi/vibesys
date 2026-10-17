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

The worktree list is a snapshot, and VibeSys adds and removes candidate worktrees inside
the project from other threads while a guarded command runs, so a worktree can appear
after the listing and before the walk finds its ``.git`` (issue #3088). The guard
therefore asks for the list again before it refuses, and a ``.git`` the walk found is
excused when the second list registers it (or a worktree above it), or when it is gone
by then. That is sound because Git registers a worktree in the repository directory
before it writes the worktree's ``.git`` and unregisters it only after deleting the
worktree: a registered worktree's ``.git`` never exists while it is unregistered. A
``.git`` neither list explains and still present is refused, as before.
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
    from collections.abc import Callable, Iterable, Sequence

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
    linked_worktrees: Callable[[], Iterable[Path]] = tuple,
) -> None:
    """Raise ``NestedRepositoryError`` for ``gitlinks`` or a ``.git`` Git would look at.

    ``pathspecs`` are those of the command about to run (none selects the whole tree).
    The top level's own ``.git`` is the repository host Git was pinned to and is allowed,
    and so is the ``.git`` of each worktree ``linked_worktrees`` lists, whose contents are
    not scanned. ``linked_worktrees`` is asked once before the walk and, when the walk
    found a ``.git`` that list does not explain, once more after it (see the module
    docstring). The walk goes by descriptor and follows no symlink (see ``WorkspaceTree``).
    """
    try:
        roots = Pathspecs(pathspecs).fixed_prefixes()
    except PathspecError:
        roots = ("",)
    top = work_tree.resolve()
    linked = _inside(top, linked_worktrees())
    found: set[str] = set()
    try:
        with open_workspace_tree(work_tree) as tree:
            for root in roots:
                found.update(tree.entries_named(_GIT, limit=SCAN_LIMIT, below=root, skip=linked))
                found.update(_above(tree, root))
            unexplained = _unexplained(found, linked)
            if unexplained:
                relisted = _inside(top, linked_worktrees())
                unexplained = {
                    path for path in _unexplained(unexplained, relisted) if _present(tree, path)
                }
    except TreeLimitError as error:
        raise NestedRepositoryError((), scan_limit=SCAN_LIMIT) from error
    if nested := sorted({*gitlinks, *unexplained}):
        raise NestedRepositoryError(nested)


def _inside(top: Path, worktrees: Iterable[Path]) -> frozenset[str]:
    """The ``worktrees`` below ``top``, relative to it."""
    resolved = (path.resolve() for path in worktrees)
    return frozenset(
        path.relative_to(top).as_posix()
        for path in resolved
        if path.is_relative_to(top) and path != top
    )


def _allowed(linked: frozenset[str]) -> frozenset[str]:
    """The ``.git`` entries host Git may meet: the top level's own and each linked worktree's."""
    return frozenset({_GIT, *(f"{path}/{_GIT}" for path in linked)})


def _unexplained(found: Iterable[str], linked: frozenset[str]) -> set[str]:
    """The ``found`` entries that are neither allowed nor inside a ``linked`` worktree."""
    allowed = _allowed(linked)
    return {
        path
        for path in found
        if path not in allowed
        and not any(PurePosixPath(path).is_relative_to(worktree) for worktree in linked)
    }


def _present(tree: WorkspaceTree, path: str) -> bool:
    """Whether ``path`` still names something, as Git would see it now."""
    try:
        return tree.kind(path) is not None
    except WorkspacePathError:
        return False  # a symlink on the way: Git does not follow it either


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
