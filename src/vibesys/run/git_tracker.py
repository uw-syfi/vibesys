"""Git history owned by one VibeSys project run."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from vs_project import Project

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vibesys.run.git_events import GitTrackerEvents
    from vs_project import GitSnapshotPlan, ProjectGitIntegration, StateSnapshot


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
            raise ValueError(f"project path must be a normalized relative path: {raw}")  # noqa: TRY003  # tracked: #288
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
    through repository-local Git configuration.
    """

    _GIT_ENV_STATIC = {  # noqa: RUF012  # tracked: #288
        "GIT_AUTHOR_NAME": "vibesys",
        "GIT_AUTHOR_EMAIL": "vibesys@local",
        "GIT_COMMITTER_NAME": "vibesys",
        "GIT_COMMITTER_EMAIL": "vibesys@local",
    }

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

    def __init__(  # noqa: D107  # tracked: #288
        self,
        root: Path,
        *,
        run_id: str,
        events: GitTrackerEvents,
        excluded_dirs: Iterable[str] = (),
        trusted_input_paths: Iterable[str | Path] = (),
    ) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"project root must be an existing directory: {self.root}")  # noqa: TRY003  # tracked: #288
        self._events = events
        self._excluded_dirs = frozenset(excluded_dirs)
        self.run_id = run_id
        self._trusted_input_paths = tuple(
            dict.fromkeys(_normalize_project_paths(trusted_input_paths))
        )
        self._trusted_input_baseline: str | None = None
        self._project_branch = f"vibesys-runs/{run_id}"
        self._state_integration: ProjectGitIntegration = Project.open(
            self.root
        ).state.git_integration(run_id)
        self._git_dir: Path | None = None
        self._work_tree: Path | None = None
        self._exclude_file = self.root / ".git" / "info" / "exclude"

    @property
    def _GIT_ENV(self) -> dict[str, str]:  # noqa: N802  # tracked: #288
        """Git env pinned to the repository selected during initialization."""
        safe_directory = self._work_tree or self.root
        config = [("safe.directory", str(safe_directory))]
        result = {
            **self._GIT_ENV_STATIC,
            "GIT_CONFIG_COUNT": str(len(config)),
        }
        for index, (key, value) in enumerate(config):
            result[f"GIT_CONFIG_KEY_{index}"] = key
            result[f"GIT_CONFIG_VALUE_{index}"] = value
        if self._git_dir is not None and self._work_tree is not None:
            result["GIT_DIR"] = str(self._git_dir)
            result["GIT_WORK_TREE"] = str(self._work_tree)
        return result

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a git command in the workspace, logging stderr on failure.

        ``timeout`` bounds the call and raises ``subprocess.TimeoutExpired``,
        which callers that must not block (a request thread, say) handle.
        """
        if env is None:
            env = os.environ.copy()
            for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
                env.pop(variable, None)
            env.update(self._GIT_ENV)
        result = subprocess.run(  # noqa: PLW1510, S603  # tracked: #288
            cmd, cwd=self.root, capture_output=True, env=env, timeout=timeout
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self._events.warning(
                f"git command failed: {' '.join(cmd)}",
                detail=f"exit code {result.returncode}: {stderr}",
            )
            result.check_returncode()
        return result

    def init(self, existing: bool, *, trusted_input_baseline: str | None = None) -> None:  # noqa: FBT001  # tracked: #288
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
        self.run(["git", "worktree", "add", "--detach", str(destination), commit])

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
        self.run(["git", "worktree", "remove", "--force", str(destination)], check=False)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        self.run(["git", "worktree", "prune"], check=False)

    def retain_candidate(self, candidate_id: str, commit: str) -> str:
        """Keep a candidate commit reachable after its worktree is removed."""
        if not self._CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError(f"invalid candidate id: {candidate_id!r}")  # noqa: TRY003  # tracked: #288
        resolved = self.run(
            ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
            check=False,
        )
        if resolved.returncode != 0:
            raise ValueError(f"candidate revision is not a commit: {commit!r}")  # noqa: TRY003  # tracked: #288
        sha = resolved.stdout.decode(errors="replace").strip()
        ref = f"refs/vibesys/{self.run_id}/candidates/{candidate_id}"
        self.run(["git", "update-ref", ref, sha])
        return ref

    def retain_worktree(self, worktree_dir: Path, candidate_id: str) -> str:
        """Retain the current commit from a caller-created local worktree."""
        destination = self._validate_local_worktree_path(worktree_dir)
        result = self._run_in_worktree(destination, ["git", "rev-parse", "HEAD"])
        return self.retain_candidate(
            candidate_id,
            result.stdout.decode(errors="replace").strip(),
        )

    def _validate_local_worktree_path(self, worktree_dir: Path) -> Path:
        """Resolve a candidate worktree path within machine-local state."""
        return self._state_integration.validate_candidate_worktree(worktree_dir)

    def _run_in_worktree(
        self,
        worktree_dir: Path,
        command: list[str],
    ) -> subprocess.CompletedProcess[bytes]:
        """Run Git against a linked worktree without the main-worktree pins."""
        env = os.environ.copy()
        for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(variable, None)
        env.update(
            {
                **self._GIT_ENV_STATIC,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(worktree_dir),
            }
        )
        result = subprocess.run(  # noqa: PLW1510, S603  # tracked: #288
            command,
            cwd=worktree_dir,
            capture_output=True,
            env=env,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"Git command failed in candidate worktree ({' '.join(command)}): {stderr}"
            )
        return result

    def snapshot(self, label: str) -> None:
        """Commit current workspace state with *label* as the commit message."""
        self._add_all()
        self._commit_staged(label)

    def candidate_patch(self, commit: str) -> str:
        """Return the candidate-owned patch from the repository baseline."""
        roots = (
            self.run(
                ["git", "rev-list", "--max-parents=0", "--reverse", commit],
            )
            .stdout.decode(errors="replace")
            .splitlines()
        )
        if not roots:
            raise ValueError(f"cannot resolve workspace baseline for commit {commit}")  # noqa: TRY003  # tracked: #288
        return self.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--full-index",
                roots[0],
                commit,
                "--",
                ".",
                *self._state_integration.metadata_restore_exclusions,
            ]
        ).stdout.decode(errors="replace")

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
                raise ValueError(f"not a commit object name: {value!r}")  # noqa: TRY003  # tracked: #288
        command = [
            "git",
            "diff",
            "--no-ext-diff",
            "--name-status",
            "--find-renames",
            "-z",
            base,
            head,
        ]
        try:
            result = self.run(command, check=False, timeout=self._READ_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._events.warning(
                f"read-only diff failed: {' '.join(command)}",
                detail=str(error),
            )
            return None
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self._events.warning(
                f"read-only diff exit {result.returncode}",
                detail=stderr,
            )
            return None
        return result.stdout.decode("utf-8", errors="replace")

    def _commit_staged(self, label: str) -> None:
        """Commit the current index, reporting the snapshot outcome."""
        # git diff --cached --quiet exits 1 when there are staged changes
        has_changes = (
            self.run(
                ["git", "diff", "--cached", "--quiet"],
                check=False,
            ).returncode
            != 0
        )
        if has_changes:
            self.run(["git", "commit", "-m", label])
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
        for state_file in plan.files:
            state_file.destination.parent.mkdir(parents=True, exist_ok=True)
            state_file.destination.write_bytes(state_file.contents)
            self.run(["git", "add", "--force", "--", state_file.pathspec])
        self._commit_staged(label)

    def snapshot_framework_metadata_only(
        self,
        label: str,
        snapshot: StateSnapshot,
    ) -> None:
        """Commit selected framework metadata without staging candidate edits."""
        plan = self._validated_framework_snapshot_plan(snapshot)
        pathspecs = [state_file.pathspec for state_file in plan.files]
        for state_file in plan.files:
            state_file.destination.parent.mkdir(parents=True, exist_ok=True)
            state_file.destination.write_bytes(state_file.contents)
        if pathspecs:
            self.run(["git", "add", "--force", "--", *pathspecs])
        has_changes = bool(pathspecs) and (
            self.run(
                ["git", "diff", "--cached", "--quiet", "--", *pathspecs],
                check=False,
            ).returncode
            != 0
        )
        if has_changes:
            self.run(["git", "commit", "--only", "-m", label, "--", *pathspecs])
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
            and state_file.destination.read_bytes() != state_file.contents
        ]
        if unexpected or mismatched:
            shown = ", ".join(sorted({*unexpected, *mismatched}))
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"refusing to overwrite unexpectedly modified committed VibeSys metadata: {shown}"
            )
        return plan

    def framework_snapshot_status(self, snapshot: StateSnapshot) -> FrameworkSnapshotStatus:
        """Compare an exact typed framework snapshot with the blobs in ``HEAD``."""
        plan = self._state_integration.resolve_snapshot(snapshot)
        matches: list[bool | None] = []
        for state_file in plan.files:
            result = self.run(
                ["git", "show", f"HEAD:{state_file.pathspec}"],
                check=False,
            )
            matches.append(None if result.returncode != 0 else result.stdout == state_file.contents)
        if matches and all(value is None for value in matches):
            return FrameworkSnapshotStatus.MISSING
        if all(value is True for value in matches):
            return FrameworkSnapshotStatus.EXACT
        return FrameworkSnapshotStatus.DIFFERENT

    @staticmethod
    def _replace_framework_namespace_contents(plan: GitSnapshotPlan) -> None:
        """Reconcile one namespace without replacing retained file inodes."""
        if plan.destination_root.exists():
            if not plan.destination_root.is_dir():
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    f"framework state namespace is not a directory: {plan.destination_root}"
                )
            retained_files = {state_file.destination for state_file in plan.files}
            retained_directories = {
                parent
                for destination in retained_files
                for parent in destination.parents
                if parent != plan.destination_root and parent.is_relative_to(plan.destination_root)
            }
            existing_paths = sorted(
                plan.destination_root.rglob("*"),
                key=lambda path: len(path.relative_to(plan.destination_root).parts),
                reverse=True,
            )
            for path in existing_paths:
                if path.is_symlink() or path.is_file():
                    if path.is_symlink() or path not in retained_files:
                        path.unlink()
                elif path.is_dir() and path not in retained_directories:
                    path.rmdir()
        for state_file in plan.files:
            state_file.destination.parent.mkdir(parents=True, exist_ok=True)
            state_file.destination.write_bytes(state_file.contents)

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
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "refusing to replace framework state while other committed "
                f"VibeSys metadata has pending changes: {', '.join(unexpected)}"
            )

        tracked = self.run(["git", "ls-files", "--", plan.scope_pathspec]).stdout.strip()
        self._replace_framework_namespace_contents(plan)
        if plan.files or tracked:
            self.run(["git", "add", "--force", "-A", "--", plan.scope_pathspec])
        has_changes = (
            self.run(
                ["git", "diff", "--cached", "--quiet", "--", plan.scope_pathspec],
                check=False,
            ).returncode
            != 0
        )
        if has_changes:
            # Commit only this namespace. Candidate edits, including edits the
            # agent staged itself, must remain pending for the candidate
            # snapshot that owns them.
            self.run(
                [
                    "git",
                    "commit",
                    "--only",
                    "-m",
                    label,
                    "--",
                    plan.scope_pathspec,
                ]
            )
            self._events.snapshot_recorded(label, commit=self.current_sha())
        else:
            self._events.snapshot_recorded(label, commit=None)

    def current_sha(self) -> str | None:
        """Return the HEAD commit sha, or ``None`` if it cannot be resolved."""
        try:
            result = self.run(["git", "rev-parse", "HEAD"], check=False)
            if result.returncode != 0:
                return None
            return result.stdout.decode(errors="replace").strip()
        except Exception:  # noqa: BLE001  # tracked: #288
            return None

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
        """
        result = self.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
            ]
        )
        prefix_result = self.run(["git", "rev-parse", "--show-prefix"])
        prefix = prefix_result.stdout.decode(errors="replace").strip()
        return sorted(
            line[3:].removeprefix(prefix) if prefix else line[3:]
            for line in result.stdout.decode(errors="replace").splitlines()
            if len(line) > 3  # noqa: PLR2004  # tracked: #288
        )

    def checkout_tree(
        self,
        sha: str,
        *,
        clean: bool = False,
        preserve_paths: Iterable[str | Path] = (),
    ) -> bool:
        """Materialize *sha*'s tree into the working directory.

        Restores both the index and worktree from *sha* so paths introduced
        after that snapshot are deleted as well as modified paths being reset.
        HEAD stays where it is, so the next commit produces a new child commit
        rather than rewriting history. With ``clean=True``, untracked files
        left over from a prior failed attempt are removed via ``git clean
        -fd``. Files below workspace-relative ``preserve_paths`` are captured
        before the restore and reapplied afterwards. This is intended for
        framework-owned memory that must survive a candidate-code rollback.
        """
        preserved: dict[Path, bytes] = {}
        try:
            preserved = self._capture_preserved_paths(preserve_paths)
            restore_cmd = [
                "git",
                "restore",
                f"--source={sha}",
                "--staged",
                "--worktree",
                "--",
                ".",
            ]
            restore_cmd.extend(self._state_integration.metadata_restore_exclusions)
            self.run(restore_cmd)
            if clean:
                clean_cmd = [
                    "git",
                    "clean",
                    "-fd",
                    "-e",
                    self._state_integration.metadata_clean_exclusion,
                ]
                clean_cmd.extend(["--", "."])
                self.run(clean_cmd, check=False)
            self._restore_preserved_paths(preserved)
            return True  # noqa: TRY300  # tracked: #288
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            try:
                self._restore_preserved_paths(preserved)
            except Exception as preserve_exc:  # noqa: BLE001  # tracked: #288
                self._events.warning(
                    "failed to restore preserved workspace memory after tree restore error",
                    detail=str(preserve_exc),
                )
            self._events.warning(
                f"git tree restore {sha[:8]} failed",
                detail=str(exc),
            )
            return False

    def _capture_preserved_paths(self, paths: Iterable[str | Path]) -> dict[Path, bytes]:
        """Read regular files below workspace-relative *paths*."""
        preserved: dict[Path, bytes] = {}
        for raw_path in paths:
            relative = Path(raw_path)
            if relative.is_absolute() or relative == Path() or ".." in relative.parts:
                raise ValueError(f"preserved path must be workspace-relative: {raw_path}")  # noqa: TRY003  # tracked: #288
            source = self.root / relative
            if source.is_file():
                preserved[relative] = source.read_bytes()
            elif source.is_dir():
                for child in source.rglob("*"):
                    if child.is_file():
                        preserved[child.relative_to(self.root)] = child.read_bytes()
        return preserved

    def _restore_preserved_paths(self, preserved: dict[Path, bytes]) -> None:
        """Reapply files captured by :meth:`_capture_preserved_paths`."""
        for relative, content in preserved.items():
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

    def trusted_input_changes(self) -> list[str]:
        """Return evaluator-owned paths changed since the trusted baseline."""
        trusted_pathspecs = self._trusted_input_pathspecs()
        initial_commit = self._trusted_input_baseline
        if initial_commit is None:
            baseline = self.run(
                [
                    "git",
                    "log",
                    "--diff-filter=A",
                    "--format=%H",
                    "--reverse",
                    "--",
                    *trusted_pathspecs,
                ]
            )
            commits = baseline.stdout.decode().splitlines()[0:1]
            if not commits:
                return ["unable to resolve the initial workspace commit"]
            initial_commit = commits[0]

        pathspec = ["--", *trusted_pathspecs]
        committed = self.run(["git", "diff", "--name-only", f"{initial_commit}..HEAD", *pathspec])
        pending = self.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                *pathspec,
            ]
        )

        prefix_result = self.run(["git", "rev-parse", "--show-prefix"])
        prefix = prefix_result.stdout.decode(errors="replace").strip()

        def workspace_relative(path: str) -> str:
            return path.removeprefix(prefix) if prefix else path

        changes = {
            workspace_relative(line)
            for line in committed.stdout.decode(errors="replace").splitlines()
            if line
        }
        changes.update(
            workspace_relative(line[3:])
            for line in pending.stdout.decode(errors="replace").splitlines()
            if len(line) > 3  # noqa: PLR2004  # tracked: #288
        )
        return sorted(changes)

    def _resolve_trusted_input_baseline(self, revision: str) -> str:
        """Resolve an operator-authorized trusted-input baseline revision.

        The revision must already be an ancestor of the resumed workspace's
        current HEAD. Pending trusted-input edits are still reported, and any
        later committed edits remain visible in the baseline-to-HEAD diff.
        """
        resolved = self.run(
            ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
            check=False,
        )
        if resolved.returncode != 0:
            raise ValueError(f"trusted input baseline {revision!r} is not a commit")  # noqa: TRY003  # tracked: #288
        commit = resolved.stdout.decode(errors="replace").strip()
        ancestor = self.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            check=False,
        )
        if ancestor.returncode != 0:
            raise ValueError(f"trusted input baseline {revision!r} is not an ancestor of HEAD")  # noqa: TRY003  # tracked: #288
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
            legacy_branch = f"vibesys/{self.run_id}"
            if not self._branch_exists(branch) and self._branch_exists(legacy_branch):
                self._project_branch = legacy_branch
                branch = legacy_branch
            self._resume_user_project(branch, trusted_input_baseline)
            return

        self._start_user_project(
            branch=branch,
            inside_work_tree=inside_work_tree,
            trusted_input_baseline=trusted_input_baseline,
        )

    def _validated_project_branch(self) -> str:
        branch = self.project_branch
        valid = self.run(["git", "check-ref-format", "--branch", branch], check=False)
        if valid.returncode != 0:
            raise ValueError(f"invalid VibeSys run id for a Git branch: {self.run_id!r}")  # noqa: TRY003  # tracked: #288
        return branch

    def _prepare_project_repository(self, *, existing: bool) -> bool:
        inside_work_tree = self._inside_work_tree()
        if not inside_work_tree:
            if existing:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    f"cannot resume VibeSys run {self.run_id!r}: no Git repository in {self.root}"
                )
            self.run(["git", "init", "-q", "-b", "main"])
            return False

        top_level = self.run(["git", "rev-parse", "--show-toplevel"])
        repository_root = Path(top_level.stdout.decode(errors="replace").strip()).resolve()
        if repository_root != self.root.resolve():
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "VibeSys Git tracking requires the input directory to be "
                f"the repository root; found containing repository {repository_root}"
            )
        return True

    def _resume_user_project(
        self,
        branch: str,
        trusted_input_baseline: str | None,
    ) -> None:
        if not self._branch_exists(branch):
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"cannot resume VibeSys run {self.run_id!r}: branch {branch!r} does not exist"
            )
        if self._current_branch() != branch:
            self._require_clean_project(
                "cannot switch to the resumed VibeSys branch with pending project changes"
            )
            self.run(["git", "switch", branch])
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
            raise ValueError("trusted input baseline is only valid when resuming a run")  # noqa: TRY003  # tracked: #288

        if inside_work_tree:
            if self.current_sha() is None:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    "existing project repository has no baseline commit"
                )
            self._require_clean_project(
                "existing project repository must be clean before starting a VibeSys run"
            )
        else:
            self._add_all()
            self.run(["git", "commit", "--allow-empty", "-m", "initial: project baseline"])

        branch_point = self.current_sha()
        if branch_point is None:
            raise ValueError("user-project baseline commit could not be resolved")  # noqa: TRY003  # tracked: #288

        if self._branch_exists(branch):
            raise ValueError(f"VibeSys run branch already exists: {branch}")  # noqa: TRY003  # tracked: #288
        self.run(["git", "switch", "-c", branch])
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
        patterns.extend(self._ARTIFACT_GITIGNORE_PATTERNS)
        self._append_exclude_patterns(patterns)

    def _trusted_input_pathspecs(self) -> tuple[str, ...]:
        return tuple(f":(literal){path.as_posix()}" for path in self._trusted_input_paths)

    def _require_private_inputs_absent_from_history(self) -> None:
        """Reject private root inputs recoverable through reachable Git objects."""
        if self.current_sha() is None:
            return
        objects = self.run(
            ["git", "rev-list", "--objects", "--all", "--reflog"],
            check=False,
        )
        if objects.returncode != 0:
            raise ValueError("cannot inspect project Git history for private inputs")  # noqa: TRY003  # tracked: #288
        private_paths = sorted(
            {
                path
                for line in objects.stdout.decode(errors="replace").splitlines()
                if (path := line.partition(" ")[2]) and self._is_private_project_input(path)
            }
        )
        if private_paths:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "project Git history contains private inputs that an optimization agent "
                f"could recover: {', '.join(private_paths)}. Remove them from history or "
                "start from a fresh repository."
            )

    @staticmethod
    def _is_private_project_input(path: str) -> bool:
        return path in {".env", "agent.toml"} or path.startswith(".env.")

    def _append_exclude_patterns(self, patterns: Iterable[str]) -> None:
        exclude_file = self._exclude_file
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text() if exclude_file.exists() else ""
        have = set(existing.splitlines())
        new = [pattern for pattern in dict.fromkeys(patterns) if pattern not in have]
        if not new:
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude_file.write_text(existing + prefix + "\n".join(new) + "\n")

    def _require_clean_project(self, message: str) -> None:
        changes = self.pending_changes()
        if changes:
            raise ValueError(f"{message}: {', '.join(changes)}")  # noqa: TRY003  # tracked: #288

    def _branch_exists(self, branch: str) -> bool:
        result = self.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        )
        return result.returncode == 0

    def _current_branch(self) -> str | None:
        result = self.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode(errors="replace").strip()

    def _pending_committed_framework_metadata(self) -> list[str]:
        result = self.run(
            ["git", "diff", "--name-only", "HEAD", "--", self._state_integration.metadata_pathspec]
        )
        return sorted(path for path in result.stdout.decode(errors="replace").splitlines() if path)

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
                full = os.path.join(dirpath, d)  # noqa: PTH118  # tracked: #288
                if os.access(full, os.R_OK | os.X_OK):
                    kept.append(d)
                else:
                    unreadable.append(os.path.relpath(full, root))
            dirnames[:] = kept  # prune unsearchable dirs from the walk
            for f in filenames:
                full = os.path.join(dirpath, f)  # noqa: PTH118  # tracked: #288
                if not os.access(full, os.R_OK):
                    unreadable.append(os.path.relpath(full, root))
        return unreadable

    @staticmethod
    def _unreadable_from_stderr(stderr: str) -> list[str]:
        """Parse paths git reported it could not index from *stderr*.

        Git prints e.g. ``error: open("foo"): Permission denied`` and
        ``error: unable to index file 'foo'``.
        """
        paths: list[str] = []
        for m in re.finditer(r'(?:open\("|unable to index file \')([^"\']+)', stderr):
            paths.append(m.group(1))  # noqa: PERF401  # tracked: #288
        return paths

    def _exclude_paths(self, rel_paths: list[str]) -> None:
        """Append *rel_paths* to the framework-owned Git exclude file."""
        rel_paths = [p for p in dict.fromkeys(rel_paths) if p]
        if not rel_paths:
            return
        exclude_file = self._exclude_file
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text() if exclude_file.exists() else ""
        have = set(existing.splitlines())
        new = [self._exclude_pattern(p) for p in rel_paths]
        new = [p for p in new if p not in have]
        if not new:
            return
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        exclude_file.write_text(existing + prefix + "\n".join(new) + "\n")
        self._events.paths_excluded(tuple(new))

    def _add_all(self) -> None:
        """``git add -A``, resilient to files the host user cannot read.

        Excludes unreadable paths up front, then retries on any residual
        permission failure (a file may appear between the scan and the add).
        """
        self._exclude_paths(self._collect_unreadable())
        if self.current_sha() is not None:
            # Discard any index mutations made by the candidate before staging
            # the exact candidate-owned path set ourselves.
            self.run(["git", "reset", "--quiet", "HEAD", "--", "."])
        add_cmd = ["git", "add", "-A", "--", "."]
        for _ in range(3):
            result = self.run(add_cmd, check=False)
            if result.returncode == 0:
                self._unstage_project_owned_paths()
                return
            stderr = result.stderr.decode(errors="replace")
            offenders = self._unreadable_from_stderr(stderr)
            if not offenders:
                break  # failure unrelated to unreadable files — surface it
            self._exclude_paths(offenders)
        # Final attempt: let run() raise with full diagnostics if it still fails.
        self.run(add_cmd)
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
        for pattern in self._ARTIFACT_GITIGNORE_PATTERNS:
            normalized = pattern.removesuffix("/")
            if pattern.endswith("/"):
                protected.append(f":(glob)**/{normalized}/**")
            else:
                protected.append(f":(glob)**/{normalized}")
        if self.current_sha() is None:
            self.run(["git", "rm", "--cached", "-r", "--ignore-unmatch", "--", *protected])
        else:
            self.run(["git", "reset", "--quiet", "HEAD", "--", *protected])

    def _bind_repository(self) -> None:
        """Pin future commands to the repository currently containing ``root``.

        Agents can run tools such as plain ``uv init`` that create a nested
        ``.git`` directory after the framework initialized tracking. Without
        explicit ``GIT_DIR``/``GIT_WORK_TREE``, later commands silently switch
        repositories based on the current directory.
        """
        git_dir = self.run(["git", "rev-parse", "--absolute-git-dir"])
        work_tree = self.run(["git", "rev-parse", "--show-toplevel"])
        self._git_dir = Path(git_dir.stdout.decode(errors="replace").strip()).resolve()
        self._work_tree = Path(work_tree.stdout.decode(errors="replace").strip()).resolve()
        exclude_file = self.run(["git", "rev-parse", "--git-path", "info/exclude"])
        exclude_path = Path(exclude_file.stdout.decode(errors="replace").strip())
        if not exclude_path.is_absolute():
            exclude_path = self.root / exclude_path
        self._exclude_file = exclude_path.resolve()

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
        result = self.run(["git", "rev-parse", "--is-inside-work-tree"], check=False)
        return result.returncode == 0 and result.stdout.decode().strip() == "true"
