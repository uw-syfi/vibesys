"""``GitRepository`` implemented in-process with libgit2 (``pygit2``).

Every *portable* operation of the contract in ``vs_project.api.git_repository``
is answered here without starting a process. The *cli-only* operations (commit
hooks and signing, checkout conflict rules, linked-worktree administration,
reflog and ``--diff-filter`` history search, byte-exact patch text, porcelain
status, ``add -A`` with pathspec magic) are delegated to a wrapped
``CliGitRepository``: libgit2 does not reproduce them exactly and agents run the
real ``git`` in the same workspaces. The contract suite runs this class and the
CLI through the same cases and compares them.

Where libgit2 differs from Git, this module has its own code instead of
accepting the difference: ``_ref_names`` (name validity), ``_pathspec``
(pathspec magic) and the subject/ordering rules below. Where a request leaves
the subset those rules cover, the call goes to the CLI so the answer stays the
one Git gives:

* a pathspec outside the documented subset (``compile_pathspecs`` is ``None``);
* ``root_commit`` of a history with several roots, ``recent_subjects`` over a
  merge, a negative ``limit``: Git's tie-breaking among equal commit times is
  not reproduced;
* index changes while the index has unmerged entries;
* ``update_ref`` on a symbolic ref (Git writes through to its target);
* ``is_valid_branch_name`` for ``@{-N}`` (expands from the checkout reflog) and
  ``has_ref_containing`` with a wildcard or empty prefix.

Every call opens the repository again, so nothing is cached and writes by other
processes (the agent's own ``git``) are visible at once. Writes use libgit2's
lock-file protocol (``index.lock``, ``<ref>.lock``, atomic rename), the same one
Git follows, so the CLI and this class can alternate on one repository.

Known differences from the CLI, none of which the project reads:

* libgit2 does not honor ``replace`` refs or grafts, and does not run external
  filter drivers (``filter=lfs``) when comparing a worktree with ``HEAD``.
* Repositories using extensions libgit2 lacks (``reftable``, SHA-256) cannot be
  opened here; use the CLI implementation for them (see ``_git_backend``).
* ``reset_index`` and ``unstage`` leave ``ORIG_HEAD`` and the ``HEAD`` reflog
  alone and store no file stat data for entries they rewrite (Git then
  re-hashes those files once); the content comparison they affect is
  unchanged. They also restore the index file's timestamp after writing (see
  ``_write_index``): libgit2's finer "racily clean" test would otherwise let Git
  miss a same-size edit made in the second an entry was recorded.
* The index is rewritten from the state read a moment earlier rather than under
  a lock taken before reading, so a ``git add`` by another process inside that
  window can be overwritten. The framework serializes its own index writes.
* ``initialize`` uses ``git init`` (templates, ``init.defaultBranch`` and
  hooks are Git's to apply).

Process-wide: constructing an instance turns libgit2's repository-owner check
off, because the CLI implementation already passes ``safe.directory`` for the
project root, and both must accept the same repositories.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pygit2
from pygit2.enums import ReferenceType, SortMode

from vs_project._cli_git_repository import CliGitRepository
from vs_project._exclude_file import append_excludes
from vs_project._pathspec import compile_pathspecs
from vs_project._ref_names import is_valid_branch_name, is_valid_ref_name
from vs_project.api.git_repository import (
    COMMIT_IDENTITY_EMAIL,
    COMMIT_IDENTITY_NAME,
    CommitSubject,
    GitCommandError,
    PatchStyle,
    RepositoryLocation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_project.api.git_repository import (
        GitFaultSink,
        Pathspec,
        Revision,
    )

_FAILURE_EXIT_CODE = 128
"""What Git exits with for a refused operation; recorded on errors raised here."""

_BRANCH_PREFIX = "refs/heads/"
_LIBGIT2_ERRORS = (pygit2.GitError, KeyError, ValueError, OSError)
"""libgit2 reports a failed lock or an unreadable file as ``OSError``, not ``GitError``."""
_REF_WILDCARDS = frozenset("*?[\\")


class Pygit2GitRepository:
    """libgit2 for the portable operations, the Git CLI for the rest."""

    def __init__(self, root: Path, *, faults: GitFaultSink) -> None:
        pygit2.option(pygit2.enums.Option.SET_OWNER_VALIDATION, 0)
        self._root = root
        self._faults = faults
        self._cli = CliGitRepository(root, faults=faults)
        self._location: RepositoryLocation | None = None
        self._exclude_file = root / ".git" / "info" / "exclude"

    # -- plumbing ------------------------------------------------------------

    def _fail(self, operation: str, detail: object) -> NoReturn:
        """Report the refusal to the fault sink and raise it (from the error being handled)."""
        self._faults.warning(
            f"git operation failed: {operation}", detail=f"{_FAILURE_EXIT_CODE}: {detail}"
        )
        raise GitCommandError(["libgit2", operation], _FAILURE_EXIT_CODE, str(detail).strip())

    def _open_at(self, directory: Path) -> pygit2.Repository:
        """The repository containing ``directory`` (discovered upward), as Git would find it."""
        found = pygit2.discover_repository(str(directory))
        if found is None:
            message = f"not a git repository (or any of the parent directories): {directory}"
            raise pygit2.GitError(message)
        repository = pygit2.Repository(found)
        repository.set_ident(COMMIT_IDENTITY_NAME, COMMIT_IDENTITY_EMAIL)
        return repository

    def _open(self) -> pygit2.Repository:
        """A fresh handle on the bound repository, or the one containing the root."""
        if self._location is None:
            return self._open_at(self._root)
        repository = pygit2.Repository(str(self._location.git_dir))
        repository.set_ident(COMMIT_IDENTITY_NAME, COMMIT_IDENTITY_EMAIL)
        return repository

    @staticmethod
    def _commit(repository: pygit2.Repository, revision: Revision) -> pygit2.Commit | None:
        """The commit ``revision`` names (tags peeled), or ``None``."""
        try:
            return repository.revparse_single(revision).peel(pygit2.Commit)
        except _LIBGIT2_ERRORS:
            return None

    @staticmethod
    def _head_tree(repository: pygit2.Repository) -> pygit2.Tree | None:
        """``HEAD``'s tree, or ``None`` when unborn."""
        if repository.head_is_unborn:
            return None
        return repository.head.peel(pygit2.Tree)

    @staticmethod
    def _empty_tree(repository: pygit2.Repository) -> pygit2.Tree:
        return repository[repository.TreeBuilder().write()].peel(pygit2.Tree)

    @staticmethod
    def _changed_paths(diff: pygit2.Diff) -> set[str]:
        paths: set[str] = set()
        for delta in diff.deltas:
            paths.add(delta.old_file.path)
            paths.add(delta.new_file.path)
        return paths

    # -- location ------------------------------------------------------------

    def is_inside_work_tree(self) -> bool:
        try:
            repository = self._open()
        except _LIBGIT2_ERRORS:
            return False
        if repository.is_bare or repository.workdir is None:
            return False
        return self._root.resolve().is_relative_to(Path(repository.workdir).resolve())

    def toplevel(self) -> Path:
        try:
            repository = self._open()
            if repository.workdir is None:
                _refuse("this operation must be run in a work tree")
            return Path(repository.workdir).resolve()
        except _LIBGIT2_ERRORS as error:
            self._fail("rev-parse --show-toplevel", error)

    def initialize(self, *, initial_branch: str) -> None:
        self._cli.initialize(initial_branch=initial_branch)

    def bind(self) -> RepositoryLocation:
        try:
            repository = self._open_at(self._root)
            if repository.workdir is None:
                _refuse("this operation must be run in a work tree")
            git_dir = Path(repository.path).resolve()
            location = RepositoryLocation(
                git_dir=git_dir, work_tree=Path(repository.workdir).resolve()
            )
            self._exclude_file = (_common_dir(git_dir) / "info" / "exclude").resolve()
        except _LIBGIT2_ERRORS as error:
            self._fail("rev-parse --absolute-git-dir", error)
        self._location = location
        self._cli.pin(location)
        return location

    # -- names ---------------------------------------------------------------

    def is_valid_branch_name(self, name: str) -> bool:
        if name.startswith("@{-"):
            return self._cli.is_valid_branch_name(name)
        return is_valid_branch_name(name)

    def is_valid_ref_name(self, name: str) -> bool:
        return is_valid_ref_name(name)

    # -- reading history -----------------------------------------------------

    def head(self) -> str | None:
        try:
            repository = self._open()
            if repository.head_is_unborn:
                return None
            return str(repository.head.peel(pygit2.Commit).id)
        except _LIBGIT2_ERRORS:
            return None

    def current_branch(self) -> str | None:
        try:
            reference = self._open().lookup_reference("HEAD")
        except _LIBGIT2_ERRORS:
            return None
        if reference.type != ReferenceType.SYMBOLIC:
            return None
        target = str(reference.target)
        return target.removeprefix(_BRANCH_PREFIX) if target.startswith(_BRANCH_PREFIX) else None

    def branch_exists(self, branch: str) -> bool:
        try:
            return self._open().references.get(f"{_BRANCH_PREFIX}{branch}") is not None
        except _LIBGIT2_ERRORS:
            return False

    def resolve_commit(self, revision: Revision) -> str | None:
        try:
            commit = self._commit(self._open(), revision)
        except _LIBGIT2_ERRORS:
            return None
        return None if commit is None else str(commit.id)

    def is_ancestor(self, ancestor: Revision, descendant: Revision) -> bool:
        try:
            repository = self._open()
            older = self._commit(repository, ancestor)
            newer = self._commit(repository, descendant)
            if older is None or newer is None:
                return False
            return older.id == newer.id or repository.descendant_of(newer.id, older.id)
        except _LIBGIT2_ERRORS:
            return False

    def root_commit(self, revision: Revision) -> str | None:
        try:
            repository = self._open()
            start = self._commit(repository, revision)
            if start is None:
                _refuse(f"bad revision '{revision}'")
            roots = [
                str(commit.id)
                for commit in repository.walk(start.id, SortMode.TIME)
                if not commit.parent_ids
            ]
        except _LIBGIT2_ERRORS as error:
            self._fail(f"rev-list --max-parents=0 {revision}", error)
        if len(roots) > 1:
            return self._cli.root_commit(revision)
        return roots[0] if roots else None

    def first_commit_adding(self, pathspecs: Sequence[Pathspec]) -> str | None:
        return self._cli.first_commit_adding(pathspecs)

    def recent_subjects(self, limit: int) -> tuple[CommitSubject, ...]:
        if limit < 0:
            return self._cli.recent_subjects(limit)
        try:
            repository = self._open()
            if repository.head_is_unborn:
                _refuse("your current branch does not have any commits yet")
            subjects: list[CommitSubject] = []
            commit = repository.head.peel(pygit2.Commit)
            while len(subjects) < limit:
                subjects.append(CommitSubject(str(commit.id), commit_subject(commit.raw_message)))
                if len(subjects) == limit or not commit.parent_ids:
                    break
                if len(commit.parent_ids) > 1:
                    # A merge: Git orders its branches by commit time, ties its own way.
                    return self._cli.recent_subjects(limit)
                commit = repository[commit.parent_ids[0]].peel(pygit2.Commit)
        except _LIBGIT2_ERRORS as error:
            self._fail("log", error)
        return tuple(subjects)

    def reachable_paths(self) -> frozenset[str]:
        return self._cli.reachable_paths()

    def read_blob(self, revision: Revision, path: str) -> bytes | None:
        try:
            repository = self._open()
            tree = repository.revparse_single(revision).peel(pygit2.Tree)
            entry = tree[path]
            return entry.data if isinstance(entry, pygit2.Blob) else None
        except _LIBGIT2_ERRORS:
            return None

    def has_ref_containing(self, commit: str, prefix: str) -> bool:
        if not prefix or _REF_WILDCARDS & set(prefix):
            return self._cli.has_ref_containing(commit, prefix)
        try:
            repository = self._open()
            wanted = self._commit(repository, commit)
            if wanted is None:
                return False
            return any(
                self._reaches(repository, name, wanted.id)
                for name in repository.listall_references()
                if _under_prefix(name, prefix)
            )
        except _LIBGIT2_ERRORS:
            return False

    def _reaches(self, repository: pygit2.Repository, name: str, wanted: pygit2.Oid) -> bool:
        """Whether the ref ``name`` ends at a commit that is, or descends from, ``wanted``."""
        try:
            tip = self._commit(repository, name)
            if tip is None:
                return False
            return tip.id == wanted or repository.descendant_of(tip.id, wanted)
        except _LIBGIT2_ERRORS:
            return False

    # -- changing refs -------------------------------------------------------

    def update_ref(self, ref: str, commit: str) -> None:
        if not ref.startswith("refs/"):
            # ``HEAD`` and other pseudo-refs are written through to their target by Git.
            self._cli.update_ref(ref, commit)
            return
        if not self.is_valid_ref_name(ref):
            self._fail(f"update-ref {ref}", f"invalid ref name: {ref}")
        try:
            repository = self._open()
            target = repository.revparse_single(commit)
            if ref.startswith(_BRANCH_PREFIX) and not isinstance(target, pygit2.Commit):
                _refuse(f"trying to write non-commit object {target.id} to branch '{ref}'")
            existing = repository.references.get(ref)
            if existing is not None and existing.type == ReferenceType.SYMBOLIC:
                self._cli.update_ref(ref, commit)
                return
            repository.references.create(ref, target.id, force=True)
        except _LIBGIT2_ERRORS as error:
            self._fail(f"update-ref {ref} {commit}", error)

    def switch_branch(self, branch: str, *, create: bool = False) -> None:
        self._cli.switch_branch(branch, create=create)

    # -- comparing -----------------------------------------------------------

    def diff_patch(
        self,
        base: Revision,
        head: Revision,
        pathspecs: Sequence[Pathspec] = (),
        *,
        style: PatchStyle = PatchStyle.REVIEW,
        timeout: float | None = None,
    ) -> str:
        return self._cli.diff_patch(base, head, pathspecs, style=style, timeout=timeout)

    def diff_name_status(
        self, base: Revision, head: Revision, *, timeout: float | None = None
    ) -> str:
        return self._cli.diff_name_status(base, head, timeout=timeout)

    def tracked_changes_since_head(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        matcher = compile_pathspecs(pathspecs)
        if matcher is None:
            return self._cli.tracked_changes_since_head(pathspecs)
        try:
            repository = self._open()
            tree = self._head_tree(repository)
            if tree is None:
                _refuse("bad revision 'HEAD'")
            # ``HEAD`` against the worktree walks the tree and the index together:
            # a path the index adds or drops differs whatever its file holds,
            # every other path differs when the worktree file differs from the tree.
            paths = self._changed_paths(tree.diff_to_workdir())
            paths |= {
                delta.new_file.path
                for delta in repository.index.diff_to_tree(tree).deltas
                if delta.status_char() in {"A", "D"}
            }
        except _LIBGIT2_ERRORS as error:
            self._fail("diff --name-only HEAD", error)
        return tuple(sorted(path for path in paths if matcher.matches(path)))

    def uncommitted_paths(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        return self._cli.uncommitted_paths(pathspecs)

    def changed_since(self, commit: Revision, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        return self._cli.changed_since(commit, pathspecs)

    def has_staged_changes(self, pathspecs: Sequence[Pathspec] = ()) -> bool:
        matcher = compile_pathspecs(pathspecs)
        if matcher is None:
            return self._cli.has_staged_changes(pathspecs)
        try:
            repository = self._open()
            index = repository.index
            if index.conflicts is not None:
                return True
            tree = self._head_tree(repository) or self._empty_tree(repository)
            changed = self._changed_paths(index.diff_to_tree(tree))
        except _LIBGIT2_ERRORS:
            return True
        return any(matcher.matches(path) for path in changed)

    def has_tracked_files(self, pathspec: Pathspec) -> bool:
        matcher = compile_pathspecs([pathspec])
        if matcher is None:
            return self._cli.has_tracked_files(pathspec)
        try:
            index = self._open().index
            return any(matcher.matches(entry.path) for entry in index)
        except _LIBGIT2_ERRORS as error:
            self._fail(f"ls-files -- {pathspec}", error)

    def worktree_matches(
        self,
        revision: Revision,
        pathspecs: Sequence[Pathspec],
        *,
        include_ignored: bool,
    ) -> bool:
        return self._cli.worktree_matches(revision, pathspecs, include_ignored=include_ignored)

    # -- index and commits ---------------------------------------------------

    def stage_all(self, pathspecs: Sequence[Pathspec], *, force: bool = False) -> None:
        self._cli.stage_all(pathspecs, force=force)

    def unstage(self, pathspecs: Sequence[Pathspec]) -> None:
        matcher = compile_pathspecs(pathspecs)
        if matcher is None:
            self._cli.unstage(pathspecs)
            return
        try:
            repository = self._open()
            index = repository.index
            if index.conflicts is not None:
                self._cli.unstage(pathspecs)
                return
            tree = self._head_tree(repository)
            if tree is None:
                for entry in [entry for entry in index if matcher.matches(entry.path)]:
                    index.remove(entry.path)
            else:
                committed = pygit2.Index()
                committed.read_tree(tree)
                for path in sorted(self._changed_paths(index.diff_to_tree(tree))):
                    if not matcher.matches(path):
                        continue
                    if path in committed:
                        index.add(committed[path])
                    else:
                        index.remove(path)
            _write_index(repository, index)
        except _LIBGIT2_ERRORS as error:
            self._fail("reset HEAD --", error)

    def commit(
        self, message: str, *, only: Sequence[Pathspec] = (), allow_empty: bool = False
    ) -> None:
        self._cli.commit(message, only=only, allow_empty=allow_empty)

    # -- restoring the worktree ----------------------------------------------

    def reset_index(self) -> None:
        try:
            repository = self._open()
            tree = self._head_tree(repository)
            if tree is None:
                _refuse("ambiguous argument 'HEAD': unknown revision")
            index = repository.index
            index.read_tree(tree)
            _write_index(repository, index)
        except _LIBGIT2_ERRORS as error:
            self._fail("reset --mixed HEAD", error)

    def clean_untracked(self, *, include_ignored: bool, protect: Pathspec) -> bool:
        return self._cli.clean_untracked(include_ignored=include_ignored, protect=protect)

    def restore_worktree(self, revision: Revision, exclude: Sequence[Pathspec] = ()) -> None:
        self._cli.restore_worktree(revision, exclude)

    # -- linked worktrees ----------------------------------------------------

    def add_worktree(self, destination: Path, commit: str) -> None:
        self._cli.add_worktree(destination, commit)

    def remove_worktree(self, destination: Path) -> None:
        self._cli.remove_worktree(destination)

    def prune_worktrees(self) -> None:
        self._cli.prune_worktrees()

    def worktree_head(self, worktree: Path) -> str:
        try:
            repository = self._open_at(worktree)
            if repository.head_is_unborn:
                _refuse("ambiguous argument 'HEAD': unknown revision")
            return str(repository.head.peel(pygit2.Commit).id)
        except _LIBGIT2_ERRORS as error:
            self._fail("rev-parse HEAD", error)

    # -- repository-local ignore rules ---------------------------------------

    def add_excludes(self, patterns: Sequence[str]) -> tuple[str, ...]:
        return append_excludes(self._exclude_file, patterns)


def _write_index(repository: pygit2.Repository, index: pygit2.Index) -> None:
    """Write ``index`` and give the file back the timestamp it had.

    Git decides whether an entry's recorded file stat can be trusted by comparing
    the entry's mtime, in whole seconds, with the index file's own mtime: an
    entry modified no later than that second is "racily clean" and its content
    is checked. libgit2 compares in nanoseconds, so it leaves entries Git would
    still check as they were and writes the index with a later timestamp; a
    same-size edit made in the second the entry was recorded then goes unseen by
    the next ``git add``. Restoring the timestamp keeps every entry exactly as
    protected as it was before this write (the entries it rewrote carry no stat
    data and are always compared by content).
    """
    path = Path(repository.path) / "index"
    try:
        before = path.stat()
    except FileNotFoundError:
        index.write()
        return
    index.write()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _refuse(message: str) -> NoReturn:
    """Fail the way libgit2 does, for a request Git would refuse."""
    raise pygit2.GitError(message)


def commit_subject(message: bytes) -> str:
    """The subject Git's ``%s`` prints: the first paragraph, its lines joined by a space.

    Leading blank lines are skipped, and trailing whitespace is dropped from
    every line (a line holding only whitespace ends the paragraph).
    """
    lines: list[str] = []
    for raw in message.decode(errors="replace").split("\n"):
        line = raw.rstrip()
        if not line:
            if lines:
                break
            continue
        lines.append(line)
    return " ".join(lines)


def _under_prefix(name: str, prefix: str) -> bool:
    """``for-each-ref`` pattern rule: ``prefix`` names the ref, a parent directory, or ends in ``/``."""
    if not name.startswith(prefix):
        return False
    return len(name) == len(prefix) or name[len(prefix)] == "/" or prefix.endswith("/")


def _common_dir(git_dir: Path) -> Path:
    """The directory holding shared files: ``git_dir``, or the main repository of a linked worktree."""
    try:
        pointer = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return git_dir
    return (git_dir / pointer).resolve()
