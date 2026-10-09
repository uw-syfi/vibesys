"""The Git operations a tracked project repository needs, as one interface.

``GitTracker`` owns *policy*: which paths are framework state, what a snapshot
or a checkpoint means, how an interrupted checkpoint is recovered. A
:class:`GitRepository` owns *mechanism*: how one repository is read and
changed. The tracker asks for semantic operations ("is ``a`` an ancestor of
``b``", "stage every change under these paths"); it never builds a command line,
so an implementation is free to run the Git CLI, call a library, or keep the
repository in memory.

Implementations: ``CliGitRepository`` (the reference, runs ``git``) and
``Pygit2GitRepository`` (libgit2 in process for the portable operations, the
CLI for the rest). The contract suite in ``libs/vs-project/tests/git_contract`` runs every case against
every registered implementation; its CLI run is the oracle for the rest.

Conventions
-----------

* One instance serves one project root, the top level of its worktree
  (``GitTracker`` requires it). Paths are POSIX paths relative to that root,
  except where a method says it takes a filesystem ``Path``.
* A *revision* is an object name (full or abbreviated hex id) or ``HEAD``.
  Revision expressions (``HEAD~1``, ``a..b``) are outside the contract.
* A *pathspec* is a ``str`` from this closed subset: a relative path (a
  directory selects everything below it, ``.`` selects the root), or a path
  carrying one magic prefix: ``:(literal)<path>``, ``:(glob)<wildmatch>``
  (``**`` crosses directories) or ``:(exclude)<path-or-wildmatch>``. Excludes
  subtract from the other pathspecs of the same call.
* Failures that mean "the repository refused" raise :class:`GitCommandError`
  (or a subclass). Absence is a value (``None``, ``False``, ``()``), never an
  exception, and an error is never mapped to an absence unless a method's
  contract says so. ``OSError`` still means the machinery itself (a missing
  ``git`` executable, an unreadable directory) is unavailable.
* Commits are authored and committed as ``COMMIT_IDENTITY_NAME
  <COMMIT_IDENTITY_EMAIL>``.
* Nothing here caches. Every read observes the repository as it is now, so
  commits made by other processes (an agent's own ``git commit``, a crash
  interrupted checkpoint) are visible immediately. Index and ref updates are
  atomic per call, as Git makes them: a crash leaves each call applied or not
  applied, never half applied, and recovery decides from ``head()`` and the
  reads alone (``GitTracker``'s checkpoint recovery compares ``head()`` with a
  journal's recorded commit).

Which operations need the CLI
-----------------------------

A library-backed implementation can serve every method marked *portable*
natively. Methods marked *cli-only* depend on Git behavior a library does not
reproduce exactly; such an implementation delegates just those to the CLI
implementation (they share the same contract suite):

* ``commit``: hooks, signing and ``commit --only`` partial-commit semantics.
* ``switch_branch``, ``restore_worktree``, ``clean_untracked``: checkout
  conflict rules and pathspec-magic excludes.
* ``add_worktree``, ``remove_worktree``, ``prune_worktrees``: linked-worktree
  administration (``--force`` removal, pruning stale entries).
* ``worktree_matches``: scratch-index comparison with pathspec magic.
* ``stage_all``: ``add -A`` with pathspec magic and unreadable-path reporting.
* ``reachable_paths``, ``first_commit_adding``: reflog walk and
  ``--diff-filter`` history search.
* ``diff_patch``, ``diff_name_status``: byte-exact CLI patch text and the
  ``--name-status -z`` rename format.
* ``uncommitted_paths``, ``changed_since``: ``status --porcelain=v1`` output.

Everything else is *portable*.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

COMMIT_IDENTITY_NAME = "vibesys"
COMMIT_IDENTITY_EMAIL = "vibesys@local"

type Revision = str
"""An object name (hex id, full or abbreviated) or ``HEAD``."""

type Pathspec = str
"""A pathspec from the subset documented in this module."""


class PatchStyle(StrEnum):
    """How ``diff_patch`` renders a diff."""

    REVIEW = "review"
    """For people: renames are detected and shown as renames."""

    EXACT = "exact"
    """For identity and replay: no rename detection, full-length object ids."""


class GitError(Exception):
    """The repository refused or could not complete an operation."""


class GitCommandError(GitError):
    """An operation failed. ``command`` names it for diagnosis, nothing parses it."""

    def __init__(self, command: Sequence[str], returncode: int, stderr: str) -> None:
        """Record the failed ``command``, its exit code, and its error output."""
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git command failed ({' '.join(self.command)}): exit {returncode}: {stderr}"
        )


class GitTimeoutError(GitError):
    """A read bounded by a ``timeout`` did not finish in time."""

    def __init__(self, command: Sequence[str], timeout: float) -> None:
        """Record the ``command`` and the ``timeout`` it exceeded."""
        self.command = tuple(command)
        self.timeout = timeout
        super().__init__(f"git command timed out after {timeout}s ({' '.join(self.command)})")


class StagingError(GitCommandError):
    """``stage_all`` failed; ``unreadable`` names the paths it could not read.

    Empty ``unreadable`` means the failure had another cause. Naming the paths
    lets the caller exclude them and retry instead of aborting.
    """

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stderr: str,
        unreadable: Sequence[str],
    ) -> None:
        """Record the failure like ``GitCommandError`` plus the ``unreadable`` paths."""
        super().__init__(command, returncode, stderr)
        self.unreadable = tuple(unreadable)


@dataclass(frozen=True)
class RepositoryLocation:
    """Where the repository lives: its Git directory and worktree top level."""

    git_dir: Path
    work_tree: Path


@dataclass(frozen=True)
class CommitSubject:
    """One commit's id and subject line."""

    sha: str
    subject: str


class GitFaultSink(Protocol):
    """Receives operational faults an implementation wants an operator to see."""

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        """A Git command failed and the failure is being raised."""
        ...


class GitRepository(Protocol):
    """Semantic Git operations on one project repository."""

    # -- location ------------------------------------------------------------

    def is_inside_work_tree(self) -> bool:
        """Whether the root lies inside a Git worktree (portable)."""
        ...

    def toplevel(self) -> Path:
        """The resolved top-level directory of the worktree containing the root.

        Raises ``GitCommandError`` outside a worktree (portable).
        """
        ...

    def initialize(self, *, initial_branch: str) -> None:
        """Create a repository at the root whose unborn ``HEAD`` is ``initial_branch``."""
        ...

    def bind(self) -> RepositoryLocation:
        """Pin every later call to the repository containing the root now.

        An agent can create a nested ``.git`` after tracking started; without
        pinning, later calls would silently switch repositories. Call once
        after ``initialize`` or at resume (portable).
        """
        ...

    # -- names ---------------------------------------------------------------

    def is_valid_branch_name(self, name: str) -> bool:
        """Whether ``name`` is a legal branch name (portable)."""
        ...

    def is_valid_ref_name(self, name: str) -> bool:
        """Whether the full ref name ``name`` is legal (portable).

        A name that starts with ``-`` is never legal.
        """
        ...

    # -- reading history -----------------------------------------------------

    def head(self) -> str | None:
        """The commit ``HEAD`` resolves to, or ``None`` when unborn or unreadable.

        Never cached; must not require starting a process in the plain
        layout, it is called many times per run (portable).
        """
        ...

    def current_branch(self) -> str | None:
        """The branch ``HEAD`` names, or ``None`` when detached (portable).

        A tag that shares the branch's name does not change the answer.
        """
        ...

    def branch_exists(self, branch: str) -> bool:
        """Whether ``refs/heads/<branch>`` exists (portable)."""
        ...

    def resolve_commit(self, revision: Revision) -> str | None:
        """The full commit id ``revision`` names, or ``None`` if it names no commit.

        A tag or other object that peels to a commit resolves to that commit
        (portable).
        """
        ...

    def is_ancestor(self, ancestor: Revision, descendant: Revision) -> bool:
        """Whether ``ancestor`` is reachable from ``descendant`` (a commit is its own ancestor).

        An unresolvable revision is ``False`` (portable).
        """
        ...

    def root_commit(self, revision: Revision) -> str | None:
        """The oldest parentless commit reachable from ``revision``, or ``None``.

        Raises ``GitCommandError`` when ``revision`` does not resolve (portable).
        """
        ...

    def first_commit_adding(self, pathspecs: Sequence[Pathspec]) -> str | None:
        """The earliest commit reachable from ``HEAD`` that added a path under ``pathspecs``.

        ``None`` when none did (cli-only).
        """
        ...

    def recent_subjects(self, limit: int) -> tuple[CommitSubject, ...]:
        """Up to ``limit`` commits reachable from ``HEAD``, newest first.

        Raises ``GitCommandError`` when ``HEAD`` is unborn (portable).
        """
        ...

    def reachable_paths(self) -> frozenset[str]:
        """Every path name in a tree of any commit reachable from any ref or reflog.

        Raises ``GitCommandError`` when the history cannot be inspected
        (cli-only).
        """
        ...

    def read_blob(self, revision: Revision, path: str) -> bytes | None:
        """The bytes of the file ``path`` in ``revision``'s tree (portable).

        ``None`` when absent, or when ``path`` names a directory.
        """
        ...

    def has_ref_containing(self, commit: str, prefix: str) -> bool:
        """Whether any ref under ``prefix`` points at a commit that reaches ``commit`` (portable)."""
        ...

    # -- changing refs -------------------------------------------------------

    def update_ref(self, ref: str, commit: str) -> None:
        """Point the full ref ``ref`` at ``commit``, creating it.

        Raises ``GitCommandError`` when ``commit`` is not an existing object
        (portable).
        """
        ...

    def switch_branch(self, branch: str, *, create: bool = False) -> None:
        """Check out ``branch`` (created at ``HEAD`` with ``create``).

        Raises ``GitCommandError`` when pending changes conflict (cli-only).
        """
        ...

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
        """The unified diff from ``base`` to ``head`` limited to ``pathspecs``.

        No external diff drivers. Raises ``GitCommandError``, or
        ``GitTimeoutError`` past ``timeout`` (cli-only).
        """
        ...

    def diff_name_status(
        self, base: Revision, head: Revision, *, timeout: float | None = None
    ) -> str:
        """The NUL-delimited ``--name-status`` diff, with rename detection.

        Raises like ``diff_patch`` (cli-only).
        """
        ...

    def tracked_changes_since_head(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        """Sorted tracked paths under ``pathspecs`` whose content differs from ``HEAD``.

        Index and worktree both count; names are reported as they are, unquoted.
        Raises ``GitCommandError`` (portable).
        """
        ...

    def uncommitted_paths(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        """Sorted paths under ``pathspecs`` that are modified, staged, or untracked.

        Ignored paths are not reported; untracked files are listed one by one,
        not as directories. Staged renames are out of contract (cli-only).
        """
        ...

    def changed_since(self, commit: Revision, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        """Sorted union of paths changed in ``commit..HEAD`` and ``uncommitted_paths``."""
        ...

    def has_staged_changes(self, pathspecs: Sequence[Pathspec] = ()) -> bool:
        """Whether the index differs from ``HEAD`` under ``pathspecs`` (all, when empty).

        An index that cannot be compared reports ``True`` (portable).
        """
        ...

    def has_tracked_files(self, pathspec: Pathspec) -> bool:
        """Whether the index tracks any file under ``pathspec`` (portable)."""
        ...

    def worktree_matches(
        self,
        revision: Revision,
        pathspecs: Sequence[Pathspec],
        *,
        include_ignored: bool,
    ) -> bool:
        """Whether the files under ``pathspecs`` are exactly ``revision``'s tree there.

        Tracked and untracked files count, ignored files only with
        ``include_ignored``. Never touches the real index. ``False`` when the
        comparison cannot be made (cli-only).
        """
        ...

    # -- index and commits ---------------------------------------------------

    def stage_all(self, pathspecs: Sequence[Pathspec], *, force: bool = False) -> None:
        """Stage every addition, modification, and deletion under ``pathspecs``.

        ``force`` includes ignored files. Raises ``StagingError`` naming any
        unreadable paths that blocked it (cli-only).
        """
        ...

    def unstage(self, pathspecs: Sequence[Pathspec]) -> None:
        """Reset the index under ``pathspecs`` to ``HEAD`` (to empty when ``HEAD`` is unborn).

        The worktree is untouched (portable).
        """
        ...

    def commit(
        self, message: str, *, only: Sequence[Pathspec] = (), allow_empty: bool = False
    ) -> None:
        """Commit the index on ``HEAD``'s branch, advancing it atomically.

        With ``only``, commit just those paths from the worktree and leave
        everything else staged exactly as it was. Hooks run (cli-only).
        """
        ...

    # -- restoring the worktree ----------------------------------------------

    def reset_index(self) -> None:
        """Set the index to ``HEAD``; ``HEAD`` and the worktree stay.

        Raises ``GitCommandError`` when ``HEAD`` is unborn (portable).
        """
        ...

    def clean_untracked(self, *, include_ignored: bool, protect: Pathspec) -> bool:
        """Delete untracked files and directories except ``protect``; return success.

        Best effort: ``False`` means some paths may remain (cli-only).
        """
        ...

    def restore_worktree(self, revision: Revision, exclude: Sequence[Pathspec] = ()) -> None:
        """Make tracked worktree files equal ``revision``'s, except ``exclude`` pathspecs.

        Files absent from ``revision`` are deleted; the index and ``HEAD`` stay
        (cli-only).
        """
        ...

    # -- linked worktrees ----------------------------------------------------

    def add_worktree(self, destination: Path, commit: str) -> None:
        """Create a linked worktree at ``destination`` with a detached ``HEAD`` at ``commit``.

        Its commits are reachable by id from this repository. Callers
        serialize concurrent adds (cli-only).
        """
        ...

    def remove_worktree(self, destination: Path) -> None:
        """Unregister the linked worktree at ``destination``, best effort (cli-only)."""
        ...

    def prune_worktrees(self) -> None:
        """Drop registrations of linked worktrees whose directory is gone, best effort (cli-only)."""
        ...

    def worktree_head(self, worktree: Path) -> str:
        """The commit a linked worktree's ``HEAD`` resolves to.

        Raises ``GitCommandError`` when it does not resolve (portable).
        """
        ...

    # -- repository-local ignore rules ---------------------------------------

    def add_excludes(self, patterns: Sequence[str]) -> tuple[str, ...]:
        """Add ignore patterns to the repository-local exclude rules, once each.

        Returns the patterns that were not already present, in the order given
        (portable).
        """
        ...
