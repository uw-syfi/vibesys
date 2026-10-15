"""Which Git metadata a linked worktree needs to be readable inside a sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Entries of the shared repository directory that ``git status``, ``git diff``
# and ``git log`` read. Everything else is withheld: ``hooks`` (code Git would
# run), ``logs`` (reflogs of every branch), ``worktrees`` (the index and HEAD of
# every other worktree, including other roles' candidates), ``config`` (remote
# URLs, which may embed credentials such as ``https://user:token@host/...``, plus
# ``credential.*``, ``url.*`` and ``http.*`` settings), and any other entry.
_SHARED_ENTRIES = ("objects", "refs", "packed-refs", "HEAD", "info", "shallow")


def linked_worktree_git_paths(workspace: Path) -> tuple[Path, ...]:
    """Return the out-of-tree Git paths a linked worktree needs, for read-only binding.

    A linked worktree's ``.git`` is a file naming its own gitdir
    (``<repo>/.git/worktrees/<name>``), whose ``commondir`` names the shared
    repository directory. Both live outside the workspace, so without them every
    ``git`` command in the sandbox fails with "not a git repository". The result
    is the worktree's own gitdir plus only the shared entries listed in
    ``_SHARED_ENTRIES`` that exist, so other worktrees' metadata, reflogs and
    hooks stay invisible. ``config`` is never among them, so a credential
    embedded in a remote URL or a ``credential.*``/``url.*``/``http.*`` setting
    never reaches the sandbox: ``rev-parse``, ``status`` and ``diff`` all work
    with no repository-level config file present, at the cost of any
    repository-specific override (for example ``core.autocrlf``) that config
    would otherwise have supplied.

    Limit: Git addresses objects by hash and refs by name inside one shared
    object store and ref namespace, so ``objects`` and ``refs`` still expose every
    commit and branch of the repository to read access. Hiding them would need a
    separate clone per sandbox, which is a different design. History stays
    immutable because every path is bound read-only.

    The agent can write the workspace's ``.git`` file, so its contents only
    propose a gitdir; nothing is bound unless Git's own registration, kept outside
    the workspace, confirms it. The proposed gitdir must be an entry of a
    ``worktrees`` directory, its ``commondir`` must name that directory's parent,
    that parent must hold a repository (``HEAD`` and ``objects``), and its
    ``gitdir`` back-pointer, written by ``git worktree add``, must name this
    workspace's ``.git``. Any other contents yield an empty tuple, so Git fails
    inside the sandbox rather than the agent choosing a host path to mount.

    A registration the agent forged in a directory it can write passes those checks,
    so no shared entry may be a symlink: Git never makes one, and the bind would
    follow it to whatever host path the agent named (``objects -> ~/.ssh``). A
    symlinked entry yields an empty tuple.

    A workspace whose ``.git`` is a directory, a symlink, or missing needs nothing
    extra and yields an empty tuple.
    """
    gitdir = _registered_gitdir(workspace)
    if gitdir is None:
        return ()
    common = gitdir.parent.parent
    shared = [common / name for name in _SHARED_ENTRIES]
    if any(path.is_symlink() for path in shared):
        return ()
    return (gitdir, *(path for path in shared if path.exists()))


def _registered_gitdir(workspace: Path) -> Path | None:
    """The gitdir the workspace's ``.git`` names, when Git registered it for this workspace."""
    pointer = workspace / ".git"
    proposed = _named_path(workspace, ".git", prefix="gitdir:")
    if proposed is None or proposed.is_relative_to(workspace.resolve()):
        return None
    common = proposed.parent.parent
    registered = (
        proposed.parent.name == "worktrees"
        and (common / "HEAD").is_file()
        and (common / "objects").is_dir()
        and _named_path(proposed, "commondir") == common
        and _named_path(proposed, "gitdir") == pointer.resolve()
    )
    return proposed if registered else None


def _named_path(directory: Path, name: str, *, prefix: str = "") -> Path | None:
    """The path the one-line file ``directory/name`` holds after ``prefix``, resolved.

    ``None`` when the file is missing, a symlink, or does not start with ``prefix``.
    """
    record = directory / name
    if record.is_symlink() or not record.is_file():
        return None
    line = record.read_text(encoding="utf-8").strip()
    if not line.startswith(prefix):
        return None
    return (directory / line.removeprefix(prefix).strip()).resolve()
