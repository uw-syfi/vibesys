"""Git history owned by one VibeSys project run."""

from __future__ import annotations

import os
import re
import shutil
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_project._framework_writes import FRAMEWORK_WRITES
from vs_project._state_io import STATE_DOCUMENT_MAX_BYTES
from vs_project._workspace_tree import (
    EntryKind,
    NotRegularFileError,
    WorkspacePathError,
    open_workspace_tree,
)
from vs_project.api.git_repository import GitError, PatchStyle, StagingError
from vs_project.project import Project

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vs_project._git_events import GitTrackerEvents
    from vs_project._state import GitSnapshotPlan, ProjectGitIntegration, StateSnapshot
    from vs_project._workspace_tree import WorkspaceTree
    from vs_project.api.git_repository import GitRepository, RepositoryLocation


def _normalize_project_paths(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    """Normalize safe repository-relative paths used in literal Git pathspecs."""
    normalized: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if (
            path.is_absolute()
            or path == Path()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            message = f"project path must be a normalized relative path: {raw}"
            raise ValueError(message)
        normalized.append(path)
    return tuple(normalized)


class FrameworkSnapshotStatus(StrEnum):
    """Relationship between an expected framework snapshot and Git ``HEAD``."""

    MISSING = "missing"
    EXACT = "exact"
    DIFFERENT = "different"


class GitTracker:
    """Snapshot tracking for one canonical project repository.

    The project root is also the Git worktree root. Each run advances its own
    ``vibesys-runs/<run-id>`` branch. Machine-local framework state is excluded
    through repository-local Git configuration. ``excluded_dirs`` and
    ``excluded_files`` name directories and files, at any depth, that are
    framework inputs rather than candidate content and are never committed.

    The tracker holds policy only (what to snapshot, exclude, protect, and how a
    checkpoint is validated); every Git operation goes through ``repository``, a
    :class:`~vs_project.api.git_repository.GitRepository` over ``root``; the wiring
    code builds it, usually with ``open_git_repository`` (see ``_git_backend``).
    """

    # Compiled-accelerator artifacts an agent may emit into the workspace.
    # Large and never wanted in a per-round checkpoint. The Neuron compile cache
    # is bind-mounted *outside* the workspace, but a stray trace/compile call
    # pointed at the workspace (or a torch.compile dump) would otherwise be
    # committed and bloat history across rounds.
    _ARTIFACT_GITIGNORE_PATTERNS: tuple[str, ...] = (
        "*.neff",
        "*.ntff",
        "*.neuron",
        "*.otlp.ndjson",
        "neuroncc_compile_workdir/",
        "neuron-compile-cache/",
    )

    _PROJECT_LOCAL_EXCLUDE_PATTERNS: tuple[str, ...] = (
        "/.codex-tmp/",
        "/.env",
        "/.env.*",
        "/agent.toml",
        "*.py[co]",
        "__pycache__/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
    )

    _CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    # Abbreviated or full object name. Deliberately not a revision expression:
    # read helpers accept only values that cannot be read as a git option.
    _OBJECT_NAME = re.compile(r"^[0-9a-f]{7,64}$")

    # Bound on a read-only history query, so a wedged git cannot hold a
    # caller's thread (a frontend request thread, for instance) open forever.
    _READ_TIMEOUT_SECONDS = 10.0

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-415556 [PLR0913]; all but root are keyword-only; grouping the two exclusion sets into a value object would change every caller for no added safety.
        self,
        root: Path,
        *,
        run_id: str,
        events: GitTrackerEvents,
        excluded_dirs: Iterable[str] = (),
        excluded_files: Iterable[str] = (),
        trusted_input_paths: Iterable[str | Path] = (),
        repository: GitRepository,
    ) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            message = f"project root must be an existing directory: {self.root}"
            raise ValueError(message)
        self._events = events
        self._excluded_dirs = frozenset(excluded_dirs)
        self._excluded_files = frozenset(excluded_files)
        self.run_id = run_id
        self._trusted_input_paths = tuple(
            dict.fromkeys(_normalize_project_paths(trusted_input_paths))
        )
        self._trusted_input_baseline: str | None = None
        self._project_branch = f"vibesys-runs/{run_id}"
        self._state_integration: ProjectGitIntegration = Project.open(
            self.root
        ).state.git_integration(run_id)
        self._git = repository
        self._work_tree: Path | None = None

    def init(self, *, existing: bool, trusted_input_baseline: str | None = None) -> None:
        """Create a run branch, or resume the existing branch for this run."""
        self._init_project(
            existing=existing,
            trusted_input_baseline=trusted_input_baseline,
        )

    def add_worktree(self, worktree_dir: Path, commit: str) -> None:
        """Create a detached linked worktree at *commit*.

        The worktree gets its own working tree, index, and (detached) HEAD but
        shares this repository's object store, so a commit made in the worktree
        is immediately reachable by sha from the main repo — exactly what a
        per-candidate evolve worktree needs (isolated edits, one shared
        lineage). ``git worktree add`` mutates the main repo's
        ``.git/worktrees`` admin area, so callers must serialize concurrent
        adds; committing *inside* a worktree afterwards is independent per
        worktree and safe to run concurrently.
        """
        destination = self._validate_local_worktree_path(worktree_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._git.add_worktree(destination, commit)

    def linked_worktree_location(self, worktree_dir: Path) -> RepositoryLocation:
        """Where this repository registered the linked worktree ``worktree_dir``.

        Read from this repository's records, never from ``worktree_dir/.git``, which
        the agent working there can rewrite. Pin the worktree's own repository to it
        (``GitRepository.pin``) so host Git never follows a redirected ``.git``.
        """
        return self._git.linked_worktree_location(self._validate_local_worktree_path(worktree_dir))

    def remove_worktree(self, worktree_dir: Path) -> None:
        """Unregister a linked worktree and delete its directory (best-effort).

        ``git worktree remove`` unregisters the worktree, but it can leave the
        directory on disk — e.g. when the editor container wrote scratch files
        (``__pycache__``, ``.pytest_cache``) into the bind-mounted tree that
        ``git`` then declines to delete. Follow up with an explicit recursive
        delete so per-candidate workspaces don't accumulate across a run, then
        prune any stale admin entry. Both the ``git`` failure and any
        undeletable leftovers are non-fatal: unregistration is what matters for
        correctness, so ``ignore_errors`` keeps a stubborn file from sinking the
        run.
        """
        destination = self._validate_local_worktree_path(worktree_dir)
        self._git.remove_worktree(destination)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        self._git.prune_worktrees()

    def retain_candidate(self, candidate_id: str, commit: str) -> str:
        """Keep a candidate commit reachable after its worktree is removed."""
        if not self._CANDIDATE_ID.fullmatch(candidate_id):
            message = f"invalid candidate id: {candidate_id!r}"
            raise ValueError(message)
        sha = self._git.resolve_commit(commit)
        if sha is None:
            message = f"candidate revision is not a commit: {commit!r}"
            raise ValueError(message)
        ref = f"refs/vibesys/{self.run_id}/candidates/{candidate_id}"
        if not self._git.is_valid_ref_name(ref):
            message = f"candidate id is not a valid Git ref name component: {candidate_id!r}"
            raise ValueError(message)
        self._git.update_ref(ref, sha)
        return ref

    def is_retained(self, commit: str) -> bool:
        """Whether this run keeps ``commit`` reachable, as opposed to merely present.

        A commit is retained when it is an ancestor of the root checkout's HEAD or
        of the trusted-input baseline, or of any candidate ref created by
        ``retain_candidate``. A commit that exists in the object database but that
        no ref reaches (a dangling commit) is not retained, and neither is an
        unknown one. Read-only; a malformed object name raises ``ValueError``.
        """
        if self._OBJECT_NAME.fullmatch(commit) is None:
            message = f"not a commit object name: {commit!r}"
            raise ValueError(message)
        sha = self._git.resolve_commit(commit)
        if sha is None:
            return False
        anchors = [anchor for anchor in ("HEAD", self._trusted_input_baseline) if anchor]
        if any(self._git.is_ancestor(sha, anchor) for anchor in anchors):
            return True
        return self._git.has_ref_containing(sha, f"refs/vibesys/{self.run_id}/candidates/")

    def has_revision(self, revision: str) -> bool:
        """Whether *revision* names a commit present in this repository.

        Only an object name can: anything else (a branch, ``HEAD~1``, an option, a string
        with a NUL byte) is not a commit this run holds, so Git is never asked about it.
        """
        if self._OBJECT_NAME.fullmatch(revision) is None:
            return False
        return self._git.resolve_commit(revision) is not None

    def has_staged_changes(self) -> bool:
        """Whether the index differs from ``HEAD``."""
        return self._git.has_staged_changes()

    def is_ancestor_of_head(self, commit: str) -> bool:
        """Whether *commit* is reachable from ``HEAD`` (a commit is its own ancestor)."""
        return self._git.is_ancestor(commit, "HEAD")

    def find_snapshot(self, label: str, *, search_limit: int = 500) -> str | None:
        """Return the newest of the last *search_limit* commits whose subject is *label*."""
        try:
            subjects = self._git.recent_subjects(search_limit)
        except GitError:
            return None
        return next((entry.sha for entry in subjects if entry.subject == label), None)

    def _validate_local_worktree_path(self, worktree_dir: Path) -> Path:
        """Resolve a candidate worktree path within machine-local state."""
        return self._state_integration.validate_candidate_worktree(worktree_dir)

    def snapshot(self, label: str) -> None:
        """Commit current workspace state with *label* as the commit message."""
        self._add_all()
        self._commit_staged(label)

    def candidate_patch(self, commit: str) -> str:
        """Return the candidate-owned patch from the repository baseline."""
        root = self._git.root_commit(commit)
        if root is None:
            message = f"cannot resolve workspace baseline for commit {commit}"
            raise ValueError(message)
        return self._git.diff_patch(
            root,
            commit,
            [".", *self._state_integration.metadata_restore_exclusions],
            style=PatchStyle.EXACT,
        )

    def diff_name_status(self, base: str, head: str) -> str | None:
        """Return the NUL-delimited ``--name-status`` diff between two commits.

        Read-only: nothing about the workspace, index, or refs changes. Rename
        detection is on, so a rename arrives as one ``R<score>`` record with
        both paths. Returns None when the range does not resolve in this
        repository, when git is unavailable, or when the query outruns
        ``_READ_TIMEOUT_SECONDS``; each of those is logged rather than
        collapsing silently at the caller.

        Both arguments must be commit-ish values, not revision expressions or
        options. A value that is not a plain object name raises ``ValueError``
        so a caller cannot smuggle a flag onto the git command line.
        """
        for value in (base, head):
            if self._OBJECT_NAME.fullmatch(value) is None:
                message = f"not a commit object name: {value!r}"
                raise ValueError(message)
        try:
            return self._git.diff_name_status(base, head, timeout=self._READ_TIMEOUT_SECONDS)
        except (OSError, GitError) as error:
            self._events.warning(f"read-only diff failed: {base} {head}", detail=str(error))
            return None

    def diff_patch(self, base: str, head: str, paths: Sequence[str]) -> str | None:
        """Return a timeout-bounded unified diff for validated revisions and paths.

        This is the patch counterpart to :meth:`diff_name_status`: revisions
        must be plain object names, paths are normalized repository-relative
        values passed as literal pathspecs, and operational failures are
        reported through the tracker events before returning ``None``.
        """
        for value in (base, head):
            if self._OBJECT_NAME.fullmatch(value) is None:
                message = f"not a commit object name: {value!r}"
                raise ValueError(message)
        normalized = _normalize_project_paths(paths)
        try:
            return self._git.diff_patch(
                base,
                head,
                [f":(literal){path.as_posix()}" for path in normalized],
                timeout=self._READ_TIMEOUT_SECONDS,
            )
        except (OSError, GitError) as error:
            self._events.warning(f"read-only patch failed: {base} {head}", detail=str(error))
            return None

    def _commit_staged(self, label: str) -> None:
        """Commit the current index, reporting the snapshot outcome."""
        if self._git.has_staged_changes():
            self._git.commit(label)
            self._events.snapshot_recorded(label, commit=self.current_sha())
        else:
            self._events.snapshot_recorded(label, commit=None)

    def snapshot_with_framework_metadata(
        self,
        label: str,
        snapshot: StateSnapshot,
    ) -> None:
        """Commit candidate changes with exact framework-authored state files.

        The framework supplies complete file contents so the tracker can verify
        existing committed metadata before it writes anything. Machine-local
        runtime state is never accepted or staged.
        """
        plan = self._validated_framework_snapshot_plan(snapshot)
        self._add_all()
        self._write_framework_files(plan)
        if plan.files:
            self._git.stage_all([file.pathspec for file in plan.files], force=True)
        self._commit_staged(label)

    def snapshot_framework_metadata_only(
        self,
        label: str,
        snapshot: StateSnapshot,
    ) -> None:
        """Commit selected framework metadata without staging candidate edits."""
        plan = self._validated_framework_snapshot_plan(snapshot)
        pathspecs = [state_file.pathspec for state_file in plan.files]
        self._write_framework_files(plan)
        if pathspecs:
            self._git.stage_all(pathspecs, force=True)
        has_changes = bool(pathspecs) and self._git.has_staged_changes(pathspecs)
        if has_changes:
            self._git.commit(label, only=pathspecs)
            self._events.snapshot_recorded(label, commit=self.current_sha())
        else:
            self._events.snapshot_recorded(label, commit=None)

    def _validated_framework_snapshot_plan(
        self,
        snapshot: StateSnapshot,
    ) -> GitSnapshotPlan:
        """Resolve selected metadata after protecting unrelated framework state."""
        plan = self._state_integration.resolve_snapshot(snapshot)
        pending = self._pending_committed_framework_metadata()
        supplied = {state_file.pathspec: state_file.contents for state_file in plan.files}
        unexpected = [path for path in pending if path not in supplied]
        mismatched = [
            state_file.pathspec
            for state_file in plan.files
            if state_file.pathspec in pending
            and self._framework_bytes(state_file.destination) != state_file.contents
        ]
        if unexpected or mismatched:
            shown = ", ".join(sorted({*unexpected, *mismatched}))
            message = (
                f"refusing to overwrite unexpectedly modified committed VibeSys metadata: {shown}"
            )
            raise ValueError(message)
        return plan

    def framework_snapshot_status(self, snapshot: StateSnapshot) -> FrameworkSnapshotStatus:
        """Compare an exact typed framework snapshot with the blobs in ``HEAD``."""
        plan = self._state_integration.resolve_snapshot(snapshot)
        matches: list[bool | None] = []
        for state_file in plan.files:
            blob = self._git.read_blob("HEAD", state_file.pathspec)
            matches.append(None if blob is None else blob == state_file.contents)
        if matches and all(value is None for value in matches):
            return FrameworkSnapshotStatus.MISSING
        if all(value is True for value in matches):
            return FrameworkSnapshotStatus.EXACT
        return FrameworkSnapshotStatus.DIFFERENT

    def _framework_relative(self, destination: Path) -> str:
        """``destination``, which the plan resolved below ``root``, relative to it."""
        return destination.relative_to(self.root).as_posix()

    def _framework_bytes(self, destination: Path) -> bytes:
        """The committed framework file at ``destination``, read by descriptor."""
        with open_workspace_tree(self.root) as tree:
            return tree.read_bytes(
                self._framework_relative(destination), max_bytes=STATE_DOCUMENT_MAX_BYTES
            )

    def _write_framework_files(self, plan: GitSnapshotPlan) -> None:
        """Write the plan's files in place, by descriptor.

        The plan checked its paths for symlinks, but the agent can plant one after that;
        a descriptor walk refuses it instead of writing through it.
        """
        with open_workspace_tree(self.root) as tree:
            for state_file in plan.files:
                relative = self._framework_relative(state_file.destination)
                tree.overwrite(relative, state_file.contents)

    def _replace_framework_namespace_contents(self, plan: GitSnapshotPlan) -> None:
        """Reconcile one namespace without replacing retained file inodes."""
        namespace = self._framework_relative(plan.destination_root)
        with open_workspace_tree(self.root) as tree:
            kind = tree.kind(namespace)
            if kind is not None and kind is not EntryKind.DIRECTORY:
                message = f"framework state namespace is not a directory: {plan.destination_root}"
                raise ValueError(message)
            if kind is not None:
                retained_files = {
                    self._framework_relative(state_file.destination) for state_file in plan.files
                }
                retained_directories = {
                    parent.as_posix()
                    for destination in retained_files
                    for parent in PurePosixPath(destination).parents
                }
                deepest_first = sorted(
                    _entries_below(tree, namespace),
                    key=lambda entry: len(PurePosixPath(entry[0]).parts),
                    reverse=True,
                )
                for relative, entry_kind in deepest_first:
                    if entry_kind is EntryKind.DIRECTORY:
                        if relative not in retained_directories:
                            tree.remove(relative)
                    elif entry_kind is EntryKind.SYMLINK or relative not in retained_files:
                        tree.remove(relative)
        self._write_framework_files(plan)

    def snapshot_framework_state(
        self,
        label: str,
        snapshot: StateSnapshot,
    ) -> None:
        """Replace and commit one framework-owned run-state namespace exactly.

        Omitting a previously committed file from the validated snapshot
        authorizes its deletion. Pending tracked metadata outside the namespace
        remains protected from accidental inclusion or overwrite.
        """
        plan = self._state_integration.resolve_replacement_snapshot(snapshot)
        unexpected = [
            path
            for path in self._pending_committed_framework_metadata()
            if not plan.contains_pathspec(path)
        ]
        if unexpected:
            message = (
                "refusing to replace framework state while other committed "
                f"VibeSys metadata has pending changes: {', '.join(unexpected)}"
            )
            raise ValueError(message)

        tracked = self._git.has_tracked_files(plan.scope_pathspec)
        self._replace_framework_namespace_contents(plan)
        if plan.files or tracked:
            self._git.stage_all([plan.scope_pathspec], force=True)
        if self._git.has_staged_changes([plan.scope_pathspec]):
            # Commit only this namespace. Candidate edits, including edits the
            # agent staged itself, must remain pending for the candidate
            # snapshot that owns them.
            self._git.commit(label, only=[plan.scope_pathspec])
            self._events.snapshot_recorded(label, commit=self.current_sha())
        else:
            self._events.snapshot_recorded(label, commit=None)

    def current_sha(self) -> str | None:
        """Return the HEAD commit sha, or ``None`` if it cannot be resolved.

        Never cached: it reads the repository files on each call (no process
        spawn for the plain layout), so commits made by anyone, including an
        agent's own ``git commit``, are observed immediately.
        """
        return self._git.head()

    @property
    def history_root(self) -> Path:
        """Return the canonical project repository root."""
        return self.root

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the resolved commit used as the trusted-input baseline."""
        return self._trusted_input_baseline

    def configure_trusted_input_baseline(self, revision: str) -> str:
        """Resolve and install the persisted trusted-input baseline."""
        resolved = self._resolve_trusted_input_baseline(revision)
        self._trusted_input_baseline = resolved
        self._events.baseline_configured(resolved)
        return resolved

    def pending_changes(self) -> list[str]:
        """Return tracked and untracked workspace paths changed since ``HEAD``.

        Role-isolated agents such as the orchestrator and judge are allowed to
        inspect the candidate but not mutate it.  Callers checkpoint framework
        state first, then use this method to detect any writes the agent made
        during its turn before restoring the checkpoint.

        The framework writes its own run state below ``.vibesys`` while a turn is in
        flight, so a path is not reported when it is exactly what the framework last
        published there (see ``_framework_writes``). Any other change below
        ``.vibesys`` is reported like a change anywhere else: an isolated role must
        not rewrite framework state either.
        """
        changed = self._git.uncommitted_paths(["."])
        return [
            path for path in changed if not FRAMEWORK_WRITES.is_framework_state(self.root / path)
        ]

    def checkout_tree(
        self,
        sha: str,
        *,
        clean: bool = False,
        clean_ignored: bool = False,
        preserve_paths: Iterable[str | Path] = (),
        keep_paths: Iterable[str] = (),
    ) -> bool:
        """Materialize *sha*'s tree into the working directory.

        Restores the worktree from *sha*: paths absent from the current ``HEAD``
        are created, paths absent from *sha* are deleted, and modified paths are
        reset. The index
        is reset to ``HEAD`` and stays clean, while HEAD itself stays where it
        is. A later candidate checkpoint can therefore commit the restored
        tree as a new child instead of encountering staged changes or
        rewriting run history. With ``clean=True``, untracked files
        left over from a prior failed attempt are removed via ``git clean
        -fd``; ``clean_ignored=True`` uses ``-fdx`` so ignored files go too and
        the tree is exact. Files below workspace-relative ``preserve_paths`` are
        captured before the restore and reapplied afterwards. This is intended for
        framework-owned memory that must survive a candidate-code rollback.
        ``keep_paths`` (from :meth:`unversioned_paths`) are left exactly as they are:
        the clean skips them and nothing is copied, so they are never rewritten.
        """
        preserved: dict[Path, bytes] = {}
        preserve_paths = tuple(preserve_paths)
        kept = [_literal_anchor(path) for path in keep_paths]
        try:
            preserved = self._capture_preserved_paths(preserve_paths)
            self._git.reset_index()
            if clean:
                # Clean before restoring: restored paths that HEAD lacks are untracked,
                # so cleaning afterwards would delete them again.
                self._git.clean_untracked(
                    include_ignored=clean_ignored,
                    protect=self._state_integration.metadata_clean_exclusion,
                    # Preserved files are held only in memory until the restore ends, so a
                    # crash between the clean and the reapply must not find them deleted.
                    keep=[f"/{Path(path).as_posix().rstrip('/')}" for path in preserve_paths]
                    + kept,
                )
            # The restore leaves preserved paths as they are, so they never pass through a
            # state a crash could leave behind; reapplying them below then changes nothing.
            self._git.restore_worktree(
                sha,
                [
                    *self._state_integration.metadata_restore_exclusions,
                    *(f":(exclude){Path(path).as_posix().rstrip('/')}" for path in preserve_paths),
                ],
            )
            self._restore_preserved_paths(preserved)
        except (OSError, GitError) as exc:
            try:
                self._restore_preserved_paths(preserved)
            except OSError as preserve_exc:
                self._events.warning(
                    "failed to restore preserved workspace memory after tree restore error",
                    detail=str(preserve_exc),
                )
            self._events.warning(
                f"git tree restore {sha[:8]} failed",
                detail=str(exc),
            )
            return False
        else:
            return True

    def unversioned_paths(self) -> tuple[str, ...]:
        """The untracked and ignored files present now, as few paths as describe them.

        A directory holding no tracked file is reported once, as ``dir/``, so a
        ``.venv`` is one entry, not one per file. Pass the result as ``keep_paths`` to
        :meth:`checkout_tree` and as ``exempt_paths`` to :meth:`matches_tree`.
        """
        files = self._git.unversioned_paths(include_ignored=True)
        return tuple(self._collapse_untracked("", files))

    def _collapse_untracked(self, directory: str, files: Iterable[str]) -> list[str]:
        """Collapse *files* (all below *directory*, which ends in ``/`` or is empty)."""
        children: dict[str, list[str]] = {}
        for path in files:
            head, separator, _ = path.removeprefix(directory).partition("/")
            children.setdefault(head + separator, []).append(path)
        collapsed: list[str] = []
        for name, members in sorted(children.items()):
            if not name.endswith("/"):
                collapsed.extend(members)
            elif not self._git.has_tracked_files(f":(literal){directory}{name}"):
                collapsed.append(f"{directory}{name}")
            else:
                collapsed.extend(self._collapse_untracked(f"{directory}{name}", members))
        return collapsed

    def matches_tree(
        self,
        sha: str,
        *,
        exempt_paths: Iterable[str | Path] = (),
        include_ignored: bool = False,
    ) -> bool:
        """Return whether the working directory holds exactly *sha*'s tree.

        Tracked content and untracked files count. Ignored files count only with
        ``include_ignored=True``, which pairs with ``checkout_tree(clean_ignored=True)``.
        Trusted VibeSys files (which tree restores preserve) and files below
        workspace-relative ``exempt_paths`` are not compared.
        """
        exempt = [
            f":(exclude){_wildcard_safe(Path(path).as_posix().rstrip('/'))}"
            for path in exempt_paths
        ]
        return self._git.worktree_matches(
            sha,
            [".", *self._state_integration.metadata_restore_exclusions, *exempt],
            include_ignored=include_ignored,
        )

    def _capture_preserved_paths(self, paths: Iterable[str | Path]) -> dict[Path, bytes]:
        """Read regular files below workspace-relative *paths*, by descriptor.

        The agent writes these paths, so a path with a symlink on it or below it is
        captured not at all, never followed to a host file: the restore leaves every
        preserved path as it is on disk anyway, and the capture only guards it.
        """
        preserved: dict[Path, bytes] = {}
        with open_workspace_tree(self.root) as tree:
            for raw_path in paths:
                relative = Path(raw_path)
                if relative.is_absolute() or relative == Path() or ".." in relative.parts:
                    message = f"preserved path must be workspace-relative: {raw_path}"
                    raise ValueError(message)
                try:
                    preserved.update(_read_below(tree, relative.as_posix()))
                except (WorkspacePathError, NotRegularFileError) as error:
                    self._events.warning(
                        f"preserved path not captured: {relative}", detail=str(error)
                    )
        return preserved

    def _restore_preserved_paths(self, preserved: dict[Path, bytes]) -> None:
        """Reapply files captured by :meth:`_capture_preserved_paths`, by descriptor.

        A file whose bytes already match is left alone, and any other file is
        replaced rather than rewritten in place. An agent can leave a file it
        may not write, for example one a root container created in the
        workspace, and only the directory's permissions should decide whether
        it can be replaced, as they do for ``git restore``. A symlink the agent
        planted on the way is never followed; that file is reported, not written.
        """
        with open_workspace_tree(self.root) as tree:
            for relative, content in preserved.items():
                name = relative.as_posix()
                try:
                    if _matches(tree, name, content):
                        continue
                    tree.write_bytes(name, content)
                except WorkspacePathError as error:
                    self._events.warning(
                        f"preserved file not restored: {relative}", detail=str(error)
                    )

    def trusted_input_changes(self) -> list[str]:
        """Return evaluator-owned paths changed since the trusted baseline."""
        trusted_pathspecs = self._trusted_input_pathspecs()
        initial_commit = self._trusted_input_baseline
        if initial_commit is None:
            initial_commit = self._git.first_commit_adding(trusted_pathspecs)
            if initial_commit is None:
                return ["unable to resolve the initial workspace commit"]
        return list(self._git.changed_since(initial_commit, trusted_pathspecs))

    def _resolve_trusted_input_baseline(self, revision: str) -> str:
        """Resolve an operator-authorized trusted-input baseline revision.

        The revision must already be an ancestor of the resumed workspace's
        current HEAD. Pending trusted-input edits are still reported, and any
        later committed edits remain visible in the baseline-to-HEAD diff.
        """
        commit = self._git.resolve_commit(revision)
        if commit is None:
            message = f"trusted input baseline {revision!r} is not a commit"
            raise ValueError(message)
        if not self._git.is_ancestor(commit, "HEAD"):
            message = f"trusted input baseline {revision!r} is not an ancestor of HEAD"
            raise ValueError(message)
        return commit

    @property
    def project_branch(self) -> str:
        """Return the canonical branch name for this run."""
        return self._project_branch

    def _init_project(
        self,
        *,
        existing: bool,
        trusted_input_baseline: str | None,
    ) -> None:
        """Initialize or resume tracking directly in the project root."""
        branch = self._validated_project_branch()
        inside_work_tree = self._prepare_project_repository(existing=existing)
        self._bind_repository()
        self._install_project_excludes()
        self._require_private_inputs_absent_from_history()
        if existing:
            self._resume_user_project(branch, trusted_input_baseline)
            return

        self._start_user_project(
            branch=branch,
            inside_work_tree=inside_work_tree,
            trusted_input_baseline=trusted_input_baseline,
        )

    def _validated_project_branch(self) -> str:
        branch = self.project_branch
        if not self._git.is_valid_branch_name(branch):
            message = f"invalid VibeSys run id for a Git branch: {self.run_id!r}"
            raise ValueError(message)
        return branch

    def _prepare_project_repository(self, *, existing: bool) -> bool:
        inside_work_tree = self._inside_work_tree()
        if not inside_work_tree:
            if existing:
                message = (
                    f"cannot resume VibeSys run {self.run_id!r}: no Git repository in {self.root}"
                )
                raise ValueError(message)
            self._git.initialize(initial_branch="main")
            return False

        repository_root = self._git.toplevel()
        if repository_root != self.root.resolve():
            message = (
                "VibeSys Git tracking requires the input directory to be "
                f"the repository root; found containing repository {repository_root}"
            )
            raise ValueError(message)
        return True

    def _resume_user_project(
        self,
        branch: str,
        trusted_input_baseline: str | None,
    ) -> None:
        if not self._branch_exists(branch):
            message = f"cannot resume VibeSys run {self.run_id!r}: branch {branch!r} does not exist"
            raise ValueError(message)
        if self._current_branch() != branch:
            self._require_clean_project(
                "cannot switch to the resumed VibeSys branch with pending project changes"
            )
            self._git.switch_branch(branch)
        if trusted_input_baseline is not None:
            self.configure_trusted_input_baseline(trusted_input_baseline)

    def _start_user_project(
        self,
        *,
        branch: str,
        inside_work_tree: bool,
        trusted_input_baseline: str | None,
    ) -> None:
        if trusted_input_baseline is not None:
            message = "trusted input baseline is only valid when resuming a run"
            raise ValueError(message)

        if inside_work_tree:
            if self.current_sha() is None:
                message = "existing project repository has no baseline commit"
                raise ValueError(message)
            self._require_clean_project(
                "existing project repository must be clean before starting a VibeSys run"
            )
        else:
            self._add_all()
            self._git.commit("initial: project baseline", allow_empty=True)

        branch_point = self.current_sha()
        if branch_point is None:
            message = "user-project baseline commit could not be resolved"
            raise ValueError(message)

        if self._branch_exists(branch):
            message = f"VibeSys run branch already exists: {branch}"
            raise ValueError(message)
        self._git.switch_branch(branch, create=True)
        self._trusted_input_baseline = branch_point
        self._events.baseline_configured(branch_point)

    def _install_project_excludes(self) -> None:
        """Idempotently add local/private paths to ``.git/info/exclude``."""
        patterns = [
            self._state_integration.local_exclude_pattern,
            *self._PROJECT_LOCAL_EXCLUDE_PATTERNS,
        ]
        patterns.extend(
            f"{directory}/" for directory in sorted(self._excluded_dirs) if directory != ".git"
        )
        patterns.extend(sorted(self._excluded_files))
        patterns.extend(self._ARTIFACT_GITIGNORE_PATTERNS)
        self._git.add_excludes(list(dict.fromkeys(patterns)))

    def _trusted_input_pathspecs(self) -> tuple[str, ...]:
        return tuple(f":(literal){path.as_posix()}" for path in self._trusted_input_paths)

    def _require_private_inputs_absent_from_history(self) -> None:
        """Reject private root inputs recoverable through reachable Git objects."""
        if self.current_sha() is None:
            return
        try:
            reachable = self._git.reachable_paths()
        except GitError as error:
            message = "cannot inspect project Git history for private inputs"
            raise ValueError(message) from error
        private_paths = sorted(path for path in reachable if self._is_private_project_input(path))
        if private_paths:
            message = (
                "project Git history contains private inputs that an optimization agent "
                f"could recover: {', '.join(private_paths)}. Remove them from history or "
                "start from a fresh repository."
            )
            raise ValueError(message)

    @staticmethod
    def _is_private_project_input(path: str) -> bool:
        return path in {".env", "agent.toml"} or path.startswith(".env.")

    def _require_clean_project(self, message: str) -> None:
        changes = self.pending_changes()
        if changes:
            message = f"{message}: {', '.join(changes)}"
            raise ValueError(message)

    def _branch_exists(self, branch: str) -> bool:
        return self._git.branch_exists(branch)

    def _current_branch(self) -> str | None:
        return self._git.current_branch()

    def _pending_committed_framework_metadata(self) -> list[str]:
        return list(
            self._git.tracked_changes_since_head([self._state_integration.metadata_pathspec])
        )

    # -- snapshot resilience --------------------------------------------------
    #
    # On the Docker/Modal paths the sandbox runs as root and writes files into
    # the bind-mounted workspace.  Most land mode-644 (host-readable), but a
    # tool may emit a restrictive file the *host* user running `git add` cannot
    # read (e.g. neuron-explorer's mode-600 ``system_profile.json``).  A single
    # such file makes ``git add -A`` exit 128 and would otherwise abort the whole
    # run.  These are always transient scratch artifacts we never want in a
    # checkpoint, so we exclude them through a framework-owned exclude file
    # outside the worktree rather than fail.

    def _collect_unreadable(self) -> list[str]:
        """Workspace-relative paths the snapshotting user cannot read.

        Walks the worktree (skipping Git-ignored runtime/artifact directories,
        never following symlinks) and records files lacking ``R_OK`` and
        directories lacking ``R_OK|X_OK`` (an unsearchable dir hides its whole
        subtree from ``git add`` too). Pruning ignored trees matters because a
        Python/CUDA environment can contain gigabytes and hundreds of thousands
        of files that ``git add`` itself will never inspect.
        """
        unreadable: list[str] = []
        root = str(self.root)
        ignored_dirs = {".git", *self._excluded_dirs}
        ignored_dirs.update(
            pattern.removesuffix("/")
            for pattern in self._ARTIFACT_GITIGNORE_PATTERNS
            if pattern.endswith("/") and not set(pattern).intersection("*?[")
        )
        for dirpath, dirnames, filenames in os.walk(root):
            kept = []
            for d in dirnames:
                if d in ignored_dirs:
                    continue
                full = Path(dirpath) / d
                if os.access(full, os.R_OK | os.X_OK):
                    kept.append(d)
                else:
                    unreadable.append(os.path.relpath(full, root))
            dirnames[:] = kept  # prune unsearchable dirs from the walk
            for f in filenames:
                full = Path(dirpath) / f
                if not os.access(full, os.R_OK):
                    unreadable.append(os.path.relpath(full, root))
        return unreadable

    def _exclude_paths(self, rel_paths: list[str]) -> None:
        """Append *rel_paths* to the framework-owned Git exclude file."""
        rel_paths = [p for p in dict.fromkeys(rel_paths) if p]
        if not rel_paths:
            return
        new = self._git.add_excludes([self._exclude_pattern(p) for p in rel_paths])
        if new:
            self._events.paths_excluded(new)

    def _add_all(self) -> None:
        """``git add -A``, resilient to files the host user cannot read.

        Excludes unreadable paths up front, then retries on any residual
        permission failure (a file may appear between the scan and the add).

        Framework state is never staged here: it is unstaged right after, and
        the framework's own writers (state-store temp files that are created
        and renamed) race with git's directory scan, so ``git add`` would fail
        with "unable to stat" on a file that vanished. Excluding the subtree
        removes the race instead of retrying it; framework metadata is staged
        only through the explicit snapshot plans.
        """
        candidate_scope = [".", f":(exclude){self._state_integration.metadata_pathspec}"]
        self._exclude_paths(self._collect_unreadable())
        has_head = self.current_sha() is not None
        if has_head:
            # Discard any index mutations made by the candidate before staging
            # the exact candidate-owned path set ourselves.
            self._git.unstage(["."])
        for _ in range(3):
            try:
                self._git.stage_all(candidate_scope)
            except StagingError as error:
                if not error.unreadable:
                    raise  # failure unrelated to unreadable files: surface it
                self._exclude_paths(list(error.unreadable))
            else:
                self._unstage_project_owned_paths()
                return
        # Final attempt: report and raise with full diagnostics if it still fails.
        try:
            self._git.stage_all(candidate_scope)
        except StagingError as error:
            self._events.warning(
                f"git command failed: {' '.join(error.command)}",
                detail=f"exit code {error.returncode}: {error.stderr}",
            )
            raise
        self._unstage_project_owned_paths()

    def _unstage_project_owned_paths(self) -> None:
        """Remove framework, private, and cache paths from the candidate index."""
        protected = [
            self._state_integration.metadata_pathspec,
            ".env",
            "agent.toml",
        ]
        protected.extend(
            f":(glob)**/{directory}/**"
            for directory in sorted(self._excluded_dirs)
            if directory != ".git"
        )
        protected.extend(f":(glob)**/{name}" for name in sorted(self._excluded_files))
        for pattern in self._ARTIFACT_GITIGNORE_PATTERNS:
            normalized = pattern.removesuffix("/")
            if pattern.endswith("/"):
                protected.append(f":(glob)**/{normalized}/**")
            else:
                protected.append(f":(glob)**/{normalized}")
        self._git.unstage(protected)

    def _bind_repository(self) -> None:
        """Pin future commands to the repository currently containing ``root``.

        Agents can run tools such as plain ``uv init`` that create a nested
        ``.git`` directory after the framework initialized tracking. Without
        explicit ``GIT_DIR``/``GIT_WORK_TREE``, later commands silently switch
        repositories based on the current directory.
        """
        self._work_tree = self._git.bind().work_tree

    def _exclude_pattern(self, rel_path: str) -> str:
        """Return an exact repository-root-relative ignore pattern."""
        target = Path(rel_path)
        if self._work_tree is not None:
            try:
                workspace_prefix = self.root.resolve().relative_to(self._work_tree)
                # The proactive host scan reports workspace-relative paths,
                # while Git's stderr can report worktree-relative paths. Do
                # not apply the workspace prefix twice on the retry path.
                prefix_parts = workspace_prefix.parts
                if target.parts[: len(prefix_parts)] != prefix_parts:
                    target = workspace_prefix / target
            except ValueError:
                pass
        return "/" + target.as_posix().lstrip("/")

    def _inside_work_tree(self) -> bool:
        return self._git.is_inside_work_tree()


def _read_below(tree: WorkspaceTree, relative: str) -> dict[Path, bytes]:
    """The bytes of the regular file ``relative``, or of every one below the directory."""
    match tree.kind(relative):
        case EntryKind.FILE:
            names: tuple[str, ...] = (relative,)
        case EntryKind.DIRECTORY:
            names = tree.files_below(relative)
        case _:
            return {}
    result: dict[Path, bytes] = {}
    for name in names:
        with tree.open_file(name) as stream:
            result[Path(name)] = stream.read()
    return result


def _matches(tree: WorkspaceTree, relative: str, content: bytes) -> bool:
    """Whether ``relative`` is a regular file holding exactly ``content``."""
    if tree.kind(relative) is not EntryKind.FILE:
        return False
    return tree.read_bytes(relative, max_bytes=len(content)) == content


def _entries_below(tree: WorkspaceTree, relative: str) -> list[tuple[str, EntryKind]]:
    """Every entry below the directory ``relative`` with its kind; no symlink is followed."""
    found: list[tuple[str, EntryKind]] = []
    pending = [relative]
    while pending:
        directory = pending.pop()
        for name in tree.names(directory):
            child = f"{directory}/{name}"
            kind = tree.kind(child)
            if kind is None:
                continue
            found.append((child, kind))
            if kind is EntryKind.DIRECTORY:
                pending.append(child)
    return found


def _wildcard_safe(path: str) -> str:
    """*path* as a pattern that matches it, and at worst a few more paths, never fewer.

    Each wildcard character becomes ``?``, which matches itself, so a pattern built
    from a file name can only over-match: a path kept or exempted this way is never
    missed.
    """
    return "".join("?" if char in "*?[\\" else char for char in path)


def _literal_anchor(path: str) -> str:
    """The ``clean`` keep pattern anchored at the worktree root for *path*."""
    return "/" + _wildcard_safe(path.rstrip("/"))
