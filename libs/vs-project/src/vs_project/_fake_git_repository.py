"""``GitRepository`` implemented in memory: the Fake that runs no process and no real Git.

History (objects, refs, ``HEAD``) and the index live in memory, in a ``GitDisk`` that a
"restart" (a new ``FakeGitRepository`` over the same directory) finds again. The working
tree is the real directory, because tests and agents write files there with ordinary
file APIs. Every method reproduces what the reference ``CliGitRepository`` does for the
contract in ``vs_project.api.git_repository``, checked by the contract suite and its
differential cases; where Git's behavior is outside that contract, the Fake does what
Git does and says so below.

Documented differences from the CLI implementation:

* Commit ids are not Git's (the timestamp is a per-repository ordinal), tree and blob
  ids are.
* Patches and rename similarity come from ``difflib`` (see ``_git_textdiff``).
* ``reachable_paths`` lists every path name; the CLI lists one name per distinct object.
* Hooks, signing, and ``core.excludesFile`` do not exist.
* ``.git`` is an empty marker directory; linked worktrees get none.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from vs_project._fake_git_state import Checkout, GitDisk, Head, Repository
from vs_project._fake_git_worktree import (
    UnreadablePathError,
    WalkRequest,
    baseline_layers,
    clean_directory,
    is_ignored,
    read_entry,
    read_file,
    remove_file,
    walk_untracked,
    write_file,
)
from vs_project._git_events import NullGitTrackerEvents
from vs_project._git_objects import EMPTY_SNAPSHOT, FileEntry, Snapshot, clean_message, tree_id
from vs_project._git_pathspec import PathspecError, Pathspecs
from vs_project._git_refnames import is_valid_branch_name, is_valid_ref_name
from vs_project._git_textdiff import detect_changes, render_name_status, render_patch
from vs_project.api.git_repository import (
    CommitSubject,
    GitCommandError,
    PatchStyle,
    RepositoryLocation,
    StagingError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from vs_project._git_ignore import IgnoreLayers
    from vs_project.api.git_repository import GitFaultSink, Pathspec, Revision

_PROGRAM = "git"
"""The command name error messages show; the Fake starts no process."""
_NOT_A_REPOSITORY = "fatal: not a git repository (or any of the parent directories): .git"


@dataclass(frozen=True)
class _Scope:
    """The repository and checkout one call operates on."""

    repo: Repository
    co: Checkout

    @property
    def root(self) -> Path:
        return self.co.path

    @property
    def head(self) -> str | None:
        return self.repo.head_commit(self.co)

    @property
    def head_snapshot(self) -> Snapshot:
        return self.repo.head_snapshot(self.co)


class FakeGitRepositories(GitDisk):
    """All repositories on one in-memory disk; hands out ``FakeGitRepository`` instances.

    Build one per test world. The instance itself is a ``Callable[[Path], GitRepository]``
    factory (the shape the contract suite registers), and every repository it makes over
    the same directory serves the same state, as separate processes over one disk would.
    """

    def repository(self, root: Path, faults: GitFaultSink | None = None) -> FakeGitRepository:
        """A repository serving the worktree whose top level is ``root`` (a ``GitRepositoryFactory``)."""
        return FakeGitRepository(root, self, faults=faults or NullGitTrackerEvents())

    def __call__(self, root: Path) -> FakeGitRepository:
        """Same as ``repository(root)``."""
        return self.repository(root)


class FakeGitRepository:
    """In-memory ``GitRepository`` over a real working directory."""

    def __init__(self, root: Path, disk: FakeGitRepositories, *, faults: GitFaultSink) -> None:
        """Serve the worktree at ``root`` on ``disk``, reporting refused commands to ``faults``."""
        self._root = root
        self._disk = disk
        self._faults = faults
        self._pinned: Checkout | None = None

    # -- plumbing ---------------------------------------------------------------

    @contextmanager
    def _operation(self, name: str) -> Iterator[None]:
        """Serialize with every other operation and raise a failure queued for ``name``."""
        with self._disk.lock:
            failure = self._disk.take_failure(name)
            if failure is not None:
                raise failure
            yield

    def _refuse(
        self, command: Sequence[str], message: str, *, code: int = 128, warn: bool = True
    ) -> NoReturn:
        """Raise the error a refused ``git`` command raises, reporting it to the fault sink."""
        error = GitCommandError([_PROGRAM, *command], code, message)
        if warn:
            self._faults.warning(
                f"git command failed: git {' '.join(command)}",
                detail=f"exit code {code}: {message}",
            )
        raise error

    def _find(self) -> Checkout | None:
        if self._pinned is not None:
            alive = self._pinned in self._pinned.repo.checkouts
            return self._pinned if alive else None
        return self._disk.locate(self._root)

    def _scope(self, command: Sequence[str], *, warn: bool = True) -> _Scope:
        found = self._find()
        if found is None:
            self._refuse(command, _NOT_A_REPOSITORY, warn=warn)
        return _Scope(found.repo, found)

    def _specs(
        self, specs: Sequence[Pathspec], command: Sequence[str], *, warn: bool = True
    ) -> Pathspecs:
        try:
            return Pathspecs(specs)
        except PathspecError as error:
            self._refuse(command, f"fatal: {error}", warn=warn)

    def _commit_of(self, scope: _Scope, revision: Revision) -> str | None:
        return scope.repo.resolve(scope.co, revision)

    def _snapshot_of(self, scope: _Scope, revision: Revision) -> Snapshot | None:
        commit = self._commit_of(scope, revision)
        if commit is None:
            return None
        return scope.repo.objects.snapshot_of(commit)

    @staticmethod
    def _exclude_lines(repo: Repository) -> list[str]:
        try:
            return repo.exclude_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _layers(
        self, scope: _Scope, extra: list[str] | None = None, *, standard: bool = True
    ) -> IgnoreLayers:
        lines = self._exclude_lines(scope.repo) if standard else []
        return baseline_layers(lines, extra)

    def _skip(self, scope: _Scope) -> frozenset[str]:
        """Directories inside this worktree that are other worktrees."""
        return frozenset(
            other.path.relative_to(scope.root).as_posix()
            for other in scope.repo.checkouts
            if other is not scope.co and other.path.is_relative_to(scope.root)
        )

    def _walk(
        self,
        scope: _Scope,
        specs: Pathspecs | None,
        *,
        descend_ignored: bool = False,
    ) -> WalkRequest:
        return WalkRequest(
            root=scope.root,
            tracked=frozenset(scope.co.index),
            layers=self._layers(scope),
            specs=specs,
            descend_ignored=descend_ignored,
            skip=self._skip(scope),
        )

    @staticmethod
    def _reader(scope: _Scope, overlay: dict[str, bytes]) -> Callable[[FileEntry], bytes]:
        """Entry bytes from ``overlay`` (worktree content) or else the object store."""
        objects = scope.repo.objects
        return lambda entry: overlay.get(entry.blob) or objects.blob(entry.blob) or b""

    # -- location ------------------------------------------------------------

    def is_inside_work_tree(self) -> bool:
        with self._operation("is_inside_work_tree"):
            return self._find() is not None

    def toplevel(self) -> Path:
        with self._operation("toplevel"):
            return self._scope(["rev-parse", "--show-toplevel"]).root

    def initialize(self, *, initial_branch: str) -> None:
        with self._operation("initialize"):
            command = ["init", "-q", "-b", initial_branch]
            if self._disk.exact(self._root) is not None:
                return
            if not is_valid_branch_name(initial_branch):
                self._refuse(command, f"fatal: invalid initial branch name: '{initial_branch}'")
            root = self._root.resolve()
            git_dir = root / ".git"
            (git_dir / "info").mkdir(parents=True, exist_ok=True)
            (git_dir / "info" / "exclude").touch()
            repo = Repository(git_dir=git_dir)
            self._disk.register(Checkout(path=root, repo=repo, head=Head(initial_branch)))

    def bind(self) -> RepositoryLocation:
        with self._operation("bind"):
            scope = self._scope(["rev-parse", "--absolute-git-dir", "--show-toplevel"])
            self._pinned = scope.co
            git_dir = scope.repo.git_dir
            if scope.co.name is not None:
                git_dir = git_dir / "worktrees" / scope.co.name
            return RepositoryLocation(git_dir=git_dir, work_tree=scope.root)

    # -- names ---------------------------------------------------------------

    def is_valid_branch_name(self, name: str) -> bool:
        return is_valid_branch_name(name)

    def is_valid_ref_name(self, name: str) -> bool:
        return is_valid_ref_name(name)

    # -- reading history -----------------------------------------------------

    def head(self) -> str | None:
        with self._operation("head"):
            found = self._find()
            return None if found is None else found.repo.head_commit(found)

    def current_branch(self) -> str | None:
        with self._operation("current_branch"):
            found = self._find()
            return None if found is None else found.head.branch

    def branch_exists(self, branch: str) -> bool:
        with self._operation("branch_exists"):
            found = self._find()
            return found is not None and f"refs/heads/{branch}" in found.repo.refs

    def resolve_commit(self, revision: Revision) -> str | None:
        with self._operation("resolve_commit"):
            found = self._find()
            return None if found is None else found.repo.resolve(found, revision)

    def is_ancestor(self, ancestor: Revision, descendant: Revision) -> bool:
        with self._operation("is_ancestor"):
            found = self._find()
            if found is None:
                return False
            older = found.repo.resolve(found, ancestor)
            newer = found.repo.resolve(found, descendant)
            return (
                older is not None and newer is not None and found.repo.objects.reaches(newer, older)
            )

    def root_commit(self, revision: Revision) -> str | None:
        with self._operation("root_commit"):
            command = ["rev-list", "--max-parents=0", "--reverse", revision]
            scope = self._scope(command)
            tip = self._commit_of(scope, revision)
            if tip is None:
                self._refuse(command, f"fatal: bad revision '{revision}'")
            roots = [record for record in scope.repo.objects.ancestry(tip) if not record.parents]
            return roots[-1].id if roots else None

    def first_commit_adding(self, pathspecs: Sequence[Pathspec]) -> str | None:
        with self._operation("first_commit_adding"):
            command = ["log", "--diff-filter=A", "--format=%H", "--reverse", "--", *pathspecs]
            scope = self._scope(command)
            specs = self._specs(pathspecs, command)
            tip = scope.head
            if tip is None:
                self._refuse(command, "fatal: your current branch does not have any commits yet")
            objects = scope.repo.objects
            read = self._reader(scope, {})
            for record in reversed(objects.ancestry(tip)):
                parent = EMPTY_SNAPSHOT
                if record.parents:
                    parent = objects.snapshot_of(record.parents[0])
                changes = detect_changes(
                    _only(parent, specs), _only(objects.snapshot(record), specs), read, renames=True
                )
                if any(change.status == "A" for change in changes):
                    return record.id
            return None

    def recent_subjects(self, limit: int) -> tuple[CommitSubject, ...]:
        with self._operation("recent_subjects"):
            command = ["log", f"--max-count={limit}", "--format=%H%x1f%s"]
            scope = self._scope(command, warn=False)
            tip = scope.head
            if tip is None:
                self._refuse(
                    command, "fatal: your current branch does not have any commits yet", warn=False
                )
            history = scope.repo.objects.ancestry(tip)[: max(limit, 0)]
            return tuple(CommitSubject(sha=record.id, subject=record.subject) for record in history)

    def reachable_paths(self) -> frozenset[str]:
        with self._operation("reachable_paths"):
            scope = self._scope(["rev-list", "--objects", "--all", "--reflog"], warn=False)
            tips = set(scope.repo.refs.values())
            for checkout in scope.repo.checkouts:
                tips.update(checkout.created)
                head = scope.repo.head_commit(checkout)
                if head is not None:
                    tips.add(head)
            paths: set[str] = set()
            for tip in tips:
                for record in scope.repo.objects.ancestry(tip):
                    for path in scope.repo.objects.snapshot(record):
                        parts = path.split("/")
                        paths.update("/".join(parts[:count]) for count in range(1, len(parts) + 1))
            return frozenset(paths)

    def read_blob(self, revision: Revision, path: str) -> bytes | None:
        with self._operation("read_blob"):
            found = self._find()
            if found is None:
                return None
            snapshot = self._snapshot_of(_Scope(found.repo, found), revision)
            entry = None if snapshot is None else snapshot.get(path)
            return None if entry is None else found.repo.objects.blob(entry.blob)

    def has_ref_containing(self, commit: str, prefix: str) -> bool:
        with self._operation("has_ref_containing"):
            found = self._find()
            if found is None:
                return False
            target = found.repo.resolve(found, commit)
            if target is None:
                return False
            stem = prefix.rstrip("/")
            return any(
                (name == stem or name.startswith(f"{stem}/"))
                and found.repo.objects.reaches(tip, target)
                for name, tip in found.repo.refs.items()
            )

    def tree_of(self, revision: Revision) -> str | None:
        """The id of ``revision``'s tree, the same id real Git computes for those files.

        Not part of ``GitRepository``: commit ids carry a clock, so tests that compare
        what two runs recorded compare trees.
        """
        with self._operation("tree_of"):
            found = self._find()
            if found is None:
                return None
            commit = found.repo.resolve(found, revision)
            return None if commit is None else tree_id(found.repo.objects.snapshot_of(commit))

    # -- changing refs -------------------------------------------------------

    def update_ref(self, ref: str, commit: str) -> None:
        with self._operation("update_ref"):
            command = ["update-ref", ref, commit]
            scope = self._scope(command)
            if not ref.startswith("refs/") or not is_valid_ref_name(ref):
                self._refuse(command, f"fatal: invalid ref format: {ref}")
            target = self._commit_of(scope, commit)
            if target is None:
                self._refuse(command, f"fatal: {commit}: not a valid SHA1")
            if scope.repo.conflicts_with_existing_ref(ref):
                self._refuse(
                    command, f"error: cannot lock ref '{ref}': conflicts with an existing ref"
                )
            scope.repo.refs[ref] = target

    def switch_branch(self, branch: str, *, create: bool = False) -> None:
        with self._operation("switch_branch"):
            command = ["switch", *(["-c"] if create else []), branch]
            scope = self._scope(command)
            ref = f"refs/heads/{branch}"
            if not is_valid_branch_name(branch):
                self._refuse(command, f"fatal: invalid reference: {branch}")
            if create:
                self._create_branch(scope, command, branch)
                return
            target = scope.repo.refs.get(ref)
            if target is None:
                self._refuse(command, f"fatal: invalid reference: {branch}")
            if any(
                other is not scope.co and other.head.branch == branch
                for other in scope.repo.checkouts
            ):
                self._refuse(command, f"fatal: '{branch}' is already used by another worktree")
            self._check_out(scope, command, scope.head, target)
            scope.co.head = Head(branch)

    def _create_branch(self, scope: _Scope, command: Sequence[str], branch: str) -> None:
        ref = f"refs/heads/{branch}"
        if ref in scope.repo.refs:
            self._refuse(command, f"fatal: a branch named '{branch}' already exists")
        if scope.repo.conflicts_with_existing_ref(ref):
            self._refuse(command, f"fatal: cannot lock ref '{ref}': conflicts with an existing ref")
        head = scope.head
        if head is not None:
            scope.repo.refs[ref] = head
        scope.co.head = Head(branch)

    def _check_out(
        self, scope: _Scope, command: Sequence[str], current: str | None, target: str
    ) -> None:
        """Move the index and worktree from ``current``'s tree to ``target``'s, or refuse."""
        objects = scope.repo.objects
        old = scope.head_snapshot if current is not None else EMPTY_SNAPSHOT
        new = objects.snapshot_of(target)
        index = scope.co.index
        baseline = self._layers(scope)
        updates: dict[str, FileEntry | None] = {}
        conflicts: list[str] = []
        for path in sorted(old.keys() | new.keys()):
            before, after = old.get(path), new.get(path)
            if before == after or index.get(path) == after:
                continue
            if index.get(path) != before or self._dirty(scope, path, before, after, baseline):
                conflicts.append(path)
                continue
            updates[path] = after
        if conflicts:
            self._refuse(
                command,
                "error: Your local changes to the following files would be overwritten by "
                "checkout:\n\t" + "\n\t".join(conflicts),
                code=1,
            )
        for path, entry in updates.items():
            if entry is None:
                remove_file(scope.root, path)
                index.pop(path, None)
            else:
                write_file(scope.root, path, entry, objects.blob(entry.blob) or b"")
                index[path] = entry

    @staticmethod
    def _dirty(
        scope: _Scope,
        path: str,
        tracked: FileEntry | None,
        after: FileEntry | None,
        baseline: IgnoreLayers,
    ) -> bool:
        """Whether the worktree file at ``path`` would be lost by moving it to ``after``."""
        try:
            present = read_entry(scope.root, path)
        except UnreadablePathError:
            return True
        if tracked is None:
            return present is not None and not is_ignored(scope.root, path, baseline)
        return present != tracked and not (present is None and after is None)

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
        del timeout
        with self._operation("diff_patch"):
            command = ["diff", base, head, "--", *pathspecs]
            scope = self._scope(command, warn=False)
            specs = self._specs(pathspecs, command, warn=False)
            before, after = self._two_snapshots(scope, command, base, head)
            read = self._reader(scope, {})
            changes = detect_changes(
                _only(before, specs), _only(after, specs), read, renames=style is PatchStyle.REVIEW
            )
            return render_patch(changes, read, full_index=style is PatchStyle.EXACT)

    def diff_name_status(
        self, base: Revision, head: Revision, *, timeout: float | None = None
    ) -> str:
        del timeout
        with self._operation("diff_name_status"):
            command = ["diff", "--name-status", "-z", base, head]
            scope = self._scope(command, warn=False)
            before, after = self._two_snapshots(scope, command, base, head)
            return render_name_status(
                detect_changes(before, after, self._reader(scope, {}), renames=True)
            )

    def _two_snapshots(
        self, scope: _Scope, command: Sequence[str], base: Revision, head: Revision
    ) -> tuple[Snapshot, Snapshot]:
        before = self._snapshot_of(scope, base)
        after = self._snapshot_of(scope, head)
        if before is None:
            self._refuse(command, f"fatal: bad revision '{base}'", warn=False)
        if after is None:
            self._refuse(command, f"fatal: bad revision '{head}'", warn=False)
        return before, after

    def tracked_changes_since_head(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        with self._operation("tracked_changes_since_head"):
            command = ["diff", "--name-only", "HEAD", "--", *pathspecs]
            scope = self._scope(command)
            specs = self._specs(pathspecs, command)
            if scope.head is None:
                self._refuse(command, "fatal: ambiguous argument 'HEAD': unknown revision")
            entries, overlay, unreadable = self._read_worktree(
                scope, [path for path in scope.co.index if specs.matches(path)]
            )
            if unreadable:
                self._refuse(command, f'error: open("{unreadable[0]}"): Permission denied')
            changes = detect_changes(
                _only(scope.head_snapshot, specs),
                entries,
                self._reader(scope, overlay),
                renames=True,
            )
            return tuple(sorted(change.path for change in changes))

    def uncommitted_paths(self, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        with self._operation("uncommitted_paths"):
            command = ["status", "--porcelain=v1", "--untracked-files=all", "--", *pathspecs]
            scope = self._scope(command)
            return tuple(sorted(self._status(scope, self._specs(pathspecs, command))))

    def changed_since(self, commit: Revision, pathspecs: Sequence[Pathspec]) -> tuple[str, ...]:
        with self._operation("changed_since"):
            command = ["diff", "--name-only", f"{commit}..HEAD", "--", *pathspecs]
            scope = self._scope(command)
            specs = self._specs(pathspecs, command)
            before = self._snapshot_of(scope, commit)
            if before is None or scope.head is None:
                self._refuse(command, f"fatal: bad revision '{commit}..HEAD'")
            committed = detect_changes(
                _only(before, specs),
                _only(scope.head_snapshot, specs),
                self._reader(scope, {}),
                renames=True,
            )
            return tuple(sorted({change.path for change in committed} | self._status(scope, specs)))

    def _status(self, scope: _Scope, specs: Pathspecs) -> set[str]:
        """Paths staged, modified in the worktree, or untracked (ignored ones excluded)."""
        head = scope.head_snapshot
        index = scope.co.index
        changed: set[str] = set()
        for path in index.keys() | head.keys():
            if not specs.matches(path):
                continue
            staged = index.get(path) != head.get(path)
            if staged or (path in index and self._worktree_differs(scope, path, index[path])):
                changed.add(path)
        for found in walk_untracked(self._walk(scope, specs)):
            if not found.ignored:
                changed.add(found.path)
        return changed

    @staticmethod
    def _worktree_differs(scope: _Scope, path: str, tracked: FileEntry) -> bool:
        try:
            return read_entry(scope.root, path) != tracked
        except UnreadablePathError:
            return True

    @staticmethod
    def _read_worktree(
        scope: _Scope, paths: Sequence[str]
    ) -> tuple[dict[str, FileEntry], dict[str, bytes], list[str]]:
        """Entries and bytes of those paths that exist, and the ones that cannot be read."""
        entries: dict[str, FileEntry] = {}
        overlay: dict[str, bytes] = {}
        unreadable: list[str] = []
        for path in sorted(paths):
            try:
                found = read_file(scope.root, path)
            except UnreadablePathError:
                unreadable.append(path)
                continue
            if found is not None:
                entries[path] = found[0]
                overlay[found[0].blob] = found[1]
        return entries, overlay, unreadable

    def has_staged_changes(self, pathspecs: Sequence[Pathspec] = ()) -> bool:
        with self._operation("has_staged_changes"):
            command = ["diff", "--cached", "--quiet", *(["--", *pathspecs] if pathspecs else [])]
            found = self._find()
            if found is None:
                return True
            scope = _Scope(found.repo, found)
            specs = self._specs(pathspecs, command)
            return _only(scope.co.index, specs) != _only(scope.head_snapshot, specs)

    def has_tracked_files(self, pathspec: Pathspec) -> bool:
        with self._operation("has_tracked_files"):
            command = ["ls-files", "--", pathspec]
            scope = self._scope(command)
            specs = self._specs([pathspec], command)
            return any(specs.matches(path) for path in scope.co.index)

    def worktree_matches(
        self,
        revision: Revision,
        pathspecs: Sequence[Pathspec],
        *,
        include_ignored: bool,
    ) -> bool:
        with self._operation("worktree_matches"):
            found = self._find()
            if found is None:
                return False
            scope = _Scope(found.repo, found)
            snapshot = self._snapshot_of(scope, revision)
            try:
                specs = Pathspecs(pathspecs)
            except PathspecError:
                return False
            if snapshot is None:
                return False
            return self._matches(scope, snapshot, specs, include_ignored=include_ignored)

    def _matches(
        self, scope: _Scope, snapshot: Snapshot, specs: Pathspecs, *, include_ignored: bool
    ) -> bool:
        for path, entry in snapshot.items():
            try:
                if specs.matches(path) and read_entry(scope.root, path) != entry:
                    return False
            except UnreadablePathError:
                return False
        request = WalkRequest(
            root=scope.root,
            tracked=frozenset(snapshot),
            layers=self._layers(scope),
            specs=specs,
            descend_ignored=include_ignored,
            skip=self._skip(scope),
        )
        return all(found.ignored and not include_ignored for found in walk_untracked(request))

    # -- index and commits ---------------------------------------------------

    def stage_all(self, pathspecs: Sequence[Pathspec], *, force: bool = False) -> None:
        with self._operation("stage_all"):
            command = ["add", *(["--force"] if force else []), "-A", "--", *pathspecs]
            found = self._find()
            if found is None:
                self._staging_failure(command, 128, _NOT_A_REPOSITORY, ())
            scope = _Scope(found.repo, found)
            try:
                specs = Pathspecs(pathspecs)
            except PathspecError as error:
                self._staging_failure(command, 128, f"fatal: {error}", ())
            index = scope.co.index
            tracked = [path for path in index if specs.matches(path)]
            entries, overlay, unreadable = self._read_worktree(scope, tracked)
            updates: dict[str, FileEntry | None] = {
                path: entries.get(path) for path in tracked if entries.get(path) != index[path]
            }
            candidates = set(index)
            blocked: list[str] = []
            for found in walk_untracked(self._walk(scope, specs, descend_ignored=force)):
                candidates.add(found.path.rstrip("/"))
                if found.ignored and not force:
                    if specs.names_exactly(found.path.rstrip("/")):
                        blocked.append(found.path.rstrip("/"))
                    continue
                self._stage_found(scope, found.path, updates, overlay, unreadable)
            missing = specs.unmatched(sorted(candidates))
            if missing:
                self._staging_failure(
                    command, 128, f"fatal: pathspec '{missing[0]}' did not match any files", ()
                )
            if unreadable:
                self._refuse_unreadable(command, sorted(set(unreadable)))
            self._apply_staging(scope, updates, overlay)
            if blocked:
                self._staging_failure(
                    command,
                    1,
                    "The following paths are ignored by one of your .gitignore files:\n"
                    + "\n".join(blocked),
                    (),
                )

    def _stage_found(
        self,
        scope: _Scope,
        path: str,
        updates: dict[str, FileEntry | None],
        overlay: dict[str, bytes],
        unreadable: list[str],
    ) -> None:
        try:
            found = read_file(scope.root, path)
        except UnreadablePathError:
            unreadable.append(path)
            return
        if found is not None:
            updates[path] = found[0]
            overlay[found[0].blob] = found[1]

    def _apply_staging(
        self, scope: _Scope, updates: dict[str, FileEntry | None], overlay: dict[str, bytes]
    ) -> None:
        index = dict(scope.co.index)
        for path, entry in updates.items():
            if entry is None:
                index.pop(path, None)
            else:
                scope.repo.objects.add_blob(overlay[entry.blob])
                index[path] = entry
        scope.co.index = index

    def _refuse_unreadable(self, command: Sequence[str], unreadable: list[str]) -> NoReturn:
        stderr = "\n".join(
            f"error: open(\"{path}\"): Permission denied\nerror: unable to index file '{path}'"
            for path in unreadable
        )
        self._staging_failure(command, 128, stderr + "\nfatal: adding files failed", unreadable)

    def _staging_failure(
        self, command: Sequence[str], code: int, stderr: str, unreadable: Sequence[str]
    ) -> NoReturn:
        error = StagingError([_PROGRAM, *command], code, stderr, unreadable)
        if not unreadable:
            self._faults.warning(
                f"git command failed: git {' '.join(command)}",
                detail=f"exit code {code}: {stderr}",
            )
        raise error

    def unstage(self, pathspecs: Sequence[Pathspec]) -> None:
        with self._operation("unstage"):
            command = ["reset", "--quiet", "HEAD", "--", *pathspecs]
            scope = self._scope(command)
            specs = self._specs(pathspecs, command)
            head = scope.head_snapshot
            index = dict(scope.co.index)
            for path in index.keys() | head.keys():
                if specs.matches(path):
                    if path in head:
                        index[path] = head[path]
                    else:
                        index.pop(path, None)
            scope.co.index = index

    def commit(
        self, message: str, *, only: Sequence[Pathspec] = (), allow_empty: bool = False
    ) -> None:
        with self._operation("commit"):
            command = [
                "commit",
                *(["--allow-empty"] if allow_empty else []),
                *(["--only"] if only else []),
                "-m",
                message,
                *(["--", *only] if only else []),
            ]
            scope = self._scope(command)
            cleaned = clean_message(message)
            if not cleaned:
                self._refuse(command, "Aborting commit due to empty commit message.", code=1)
            parent = scope.head
            before = scope.head_snapshot
            tree: Snapshot = scope.co.index
            index = scope.co.index
            if only:
                tree, index = self._partial_tree(scope, command, self._specs(only, command))
            if not allow_empty and dict(tree) == dict(before):
                self._refuse(command, "nothing to commit", code=1)
            record = scope.repo.objects.add_commit(tree, (parent,) if parent else (), cleaned)
            scope.repo.advance_head(scope.co, record.id)
            scope.co.created.append(record.id)
            scope.co.index = dict(index)

    def _partial_tree(
        self, scope: _Scope, command: Sequence[str], specs: Pathspecs
    ) -> tuple[dict[str, FileEntry], dict[str, FileEntry]]:
        """The tree ``commit --only`` records and the index it leaves behind."""
        head = scope.head_snapshot
        known = set(scope.co.index) | set(head)
        missing = specs.unmatched(sorted(known))
        if missing:
            self._refuse(
                command,
                f"error: pathspec '{missing[0]}' did not match any file(s) known to git",
                code=1,
            )
        tree = dict(head)
        index = dict(scope.co.index)
        entries, overlay, unreadable = self._read_worktree(
            scope, [path for path in known if specs.matches(path)]
        )
        if unreadable:
            self._refuse(command, f'error: open("{unreadable[0]}"): Permission denied', code=1)
        for path in [path for path in known if specs.matches(path)]:
            entry = entries.get(path)
            if entry is None and (scope.root / path).is_dir():
                self._refuse(
                    command, f"error: '{path}' does not have a commit checked out", code=128
                )
            if entry is None:
                tree.pop(path, None)
                index.pop(path, None)
                continue
            scope.repo.objects.add_blob(overlay[entry.blob])
            tree[path] = index[path] = entry
        return tree, index

    # -- restoring the worktree ----------------------------------------------

    def reset_index(self) -> None:
        with self._operation("reset_index"):
            command = ["reset", "--mixed", "HEAD"]
            scope = self._scope(command)
            if scope.head is None:
                self._refuse(command, "fatal: ambiguous argument 'HEAD': unknown revision")
            scope.co.index = dict(scope.head_snapshot)

    def clean_untracked(self, *, include_ignored: bool, protect: Pathspec) -> bool:
        with self._operation("clean_untracked"):
            found = self._find()
            if found is None:
                return False
            scope = _Scope(found.repo, found)
            tracked_dirs = {
                "/".join(path.split("/")[:count])
                for path in scope.co.index
                for count in range(1, len(path.split("/")))
            }
            request = WalkRequest(
                root=scope.root,
                tracked=frozenset(scope.co.index),
                layers=self._layers(scope, [protect], standard=not include_ignored),
                use_ignore_files=not include_ignored,
                skip=self._skip(scope),
            )
            return clean_directory(request, tracked_dirs)

    def restore_worktree(self, revision: Revision, exclude: Sequence[Pathspec] = ()) -> None:
        with self._operation("restore_worktree"):
            command = ["restore", f"--source={revision}", "--worktree", "--", ".", *exclude]
            scope = self._scope(command)
            specs = self._specs([".", *exclude], command)
            snapshot = self._snapshot_of(scope, revision)
            if snapshot is None:
                self._refuse(command, f"fatal: could not resolve {revision}")
            objects = scope.repo.objects
            try:
                for path in sorted(snapshot.keys() | scope.co.index.keys()):
                    if not specs.matches(path):
                        continue
                    wanted = snapshot.get(path)
                    if wanted is None:
                        remove_file(scope.root, path)
                    elif read_entry(scope.root, path) != wanted:
                        write_file(scope.root, path, wanted, objects.blob(wanted.blob) or b"")
            except OSError as error:
                self._refuse(command, f"error: {error}")

    # -- linked worktrees ----------------------------------------------------

    def add_worktree(self, destination: Path, commit: str) -> None:
        with self._operation("add_worktree"):
            command = ["worktree", "add", "--detach", str(destination), commit]
            scope = self._scope(command)
            target = self._commit_of(scope, commit)
            if target is None:
                self._refuse(command, f"fatal: invalid reference: {commit}")
            resolved = destination.resolve()
            occupied = resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir()))
            if occupied or self._disk.exact(resolved) is not None:
                self._refuse(command, f"fatal: '{destination}' already exists")
            snapshot = scope.repo.objects.snapshot_of(target)
            resolved.mkdir(parents=True, exist_ok=True)
            checkout = Checkout(
                path=resolved,
                repo=scope.repo,
                head=Head(None, target),
                name=self._admin_name(scope.repo, resolved),
                index=dict(snapshot),
            )
            try:
                for path, entry in snapshot.items():
                    write_file(resolved, path, entry, scope.repo.objects.blob(entry.blob) or b"")
            except OSError as error:
                shutil.rmtree(resolved, ignore_errors=True)
                self._refuse(command, f"fatal: could not create the worktree: {error}")
            self._disk.register(checkout)

    @staticmethod
    def _admin_name(repo: Repository, path: Path) -> str:
        taken = {checkout.name for checkout in repo.checkouts}
        name, number = path.name, 1
        while name in taken:
            number += 1
            name = f"{path.name}{number}"
        return name

    def remove_worktree(self, destination: Path) -> None:
        with self._operation("remove_worktree"):
            checkout = self._disk.exact(destination)
            if checkout is None or checkout.name is None:
                return
            self._disk.unregister(checkout)
            shutil.rmtree(checkout.path, ignore_errors=True)

    def prune_worktrees(self) -> None:
        with self._operation("prune_worktrees"):
            for checkout in self._disk.stale():
                self._disk.unregister(checkout)

    def worktree_head(self, worktree: Path) -> str:
        with self._operation("worktree_head"):
            command = ["rev-parse", "HEAD"]
            checkout = self._disk.locate(worktree)
            if checkout is None:
                self._refuse(command, _NOT_A_REPOSITORY, warn=False)
            head = checkout.repo.head_commit(checkout)
            if head is None:
                self._refuse(
                    command, "fatal: ambiguous argument 'HEAD': unknown revision", warn=False
                )
            return head

    # -- repository-local ignore rules ---------------------------------------

    def add_excludes(self, patterns: Sequence[str]) -> tuple[str, ...]:
        with self._operation("add_excludes"):
            scope = self._scope(["rev-parse", "--git-path", "info/exclude"], warn=False)
            exclude_file = scope.repo.exclude_file
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_file.read_text() if exclude_file.exists() else ""
            have = set(existing.splitlines())
            new = tuple(pattern for pattern in dict.fromkeys(patterns) if pattern not in have)
            if not new:
                return ()
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            exclude_file.write_text(existing + prefix + "\n".join(new) + "\n")
            return new


def _only(snapshot: Snapshot, specs: Pathspecs) -> dict[str, FileEntry]:
    """The entries of ``snapshot`` that ``specs`` select."""
    return {path: entry for path, entry in snapshot.items() if specs.matches(path)}
