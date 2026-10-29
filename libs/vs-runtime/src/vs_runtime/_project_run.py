"""Private owner for one project run's durable resources and Git history."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TextIO

from pydantic import BaseModel, ConfigDict, Field

from vs_project.api import (
    GitTracker,
    GitTrackerEvents,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
    RunLogger,
    StateNamespace,
)
from vs_runtime import _boot_trace as boot_trace
from vs_runtime._objective_document import materialize_objective_document
from vs_runtime._project_materialization import copy_support_directories
from vs_runtime._recorded_environment import migrate_recorded_run_environment
from vs_runtime._run_state import RunState
from vs_runtime.contracts import OrchestrationResumeDecision
from vs_sandbox.api import CaptureExecutionIdentity
from vs_sim.api import CloseOnce, ReleaseStack, utc_datetime

if TYPE_CHECKING:
    from vs_project.api import GitRepositoryFactory, StateStoreFactory
    from vs_runtime._environment_system import EnvironmentSystem
    from vs_runtime._project_materialization import ProjectMaterializer
    from vs_sim.api import Clock

type LogEmitter = Callable[[str, TextIO], None]
type LogReady = Callable[[Path], None]
type ResumeResolver = Callable[[OrchestrationRunManifest], OrchestrationResumeDecision]
_UNSETTLED_CANDIDATES = "unsettled-candidates"
_ROOT_CAPTURE_WORKSPACE = "root"


class CaptureExecutionRecovery(Protocol):
    """Run-scoped owner that reconciles one exact workspace execution identity."""

    def reconcile(self, workspace: Path, execution: CaptureExecutionIdentity) -> bool:
        """Return true only after *execution* is validated and confirmed settled."""
        ...


class CaptureRecoverySource(Protocol):
    """Environment role that provides restart recovery without opening a session."""

    def capture_recovery(
        self, run_id: str, *, system: EnvironmentSystem
    ) -> CaptureExecutionRecovery | None: ...


class NoCaptureRecovery:
    """Default environment role for captures with no independently recoverable owner."""

    def capture_recovery(
        self, run_id: str, *, system: EnvironmentSystem
    ) -> CaptureExecutionRecovery | None:
        del run_id, system
        return None


class CaptureIntent(BaseModel):
    """Durable identity of a capture that may have reached its run environment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: str = Field(min_length=1)
    execution: CaptureExecutionIdentity


class ProjectRunMismatchKind(StrEnum):
    """Persisted facts that must agree before a run can resume."""

    TRUSTED_INPUT_BASELINE = "trusted_input_baseline"
    BRANCH = "branch"
    TASK = "task"


class ProjectRunError(RuntimeError):
    """Base class for rejected project-run resource construction."""


class ProjectRunUnsettledCandidateError(ProjectRunError):
    """A prior host could not prove a command stopped using this candidate."""

    def __init__(self, workspace_id: str) -> None:
        super().__init__(
            f"candidate workspace {workspace_id!r} is retained because its command stop "
            "could not be confirmed"
        )


class ProjectRunBaselineMissingError(ProjectRunError):
    """Git initialization did not identify a fresh run's branch point."""

    def __init__(self) -> None:
        super().__init__("Git did not provide the project run branch-point commit")


class ProjectRunMismatchError(ProjectRunError):
    """A persisted run fact disagrees with the active project or request."""

    def __init__(
        self,
        run_id: str,
        kind: ProjectRunMismatchKind,
        recorded: str | None,
        actual: str | None,
    ) -> None:
        self.run_id = run_id
        self.kind = kind
        self.recorded = recorded
        self.actual = actual
        if kind is ProjectRunMismatchKind.TRUSTED_INPUT_BASELINE:
            message = (
                f"run {run_id!r} records trusted input baseline {recorded!r}, "
                f"but the requested baseline resolves to {actual!r}"
            )
        elif kind is ProjectRunMismatchKind.BRANCH:
            message = f"run {run_id!r} records branch {recorded!r}, but Git selected {actual!r}"
        else:
            message = f"run {run_id!r} records task {recorded!r}, but task {actual!r} was selected"
        super().__init__(message)


class ProjectRunDirtyResumeError(ProjectRunError):
    """A resume-time manifest update requires a clean candidate workspace."""

    def __init__(self, pending: Iterable[str]) -> None:
        self.pending = tuple(pending)
        super().__init__(
            "commit or discard pending project changes before increasing the run limit: "
            + ", ".join(self.pending)
        )


@dataclass(frozen=True, slots=True)
class ProjectRunRequest:
    """Authoritative facts needed to open one canonical project run."""

    project_root: Path
    run_id: str
    display_name: str
    task_name: str | None
    existing: bool
    framework_version: str
    run_environment: RunEnvironmentRecord
    execution: RunExecutionRecord
    orchestration: OrchestrationDescriptor
    objective: str | None = None
    provisional_project: ProjectMaterializer | None = None
    excluded_dirs: frozenset[str] = frozenset()
    excluded_files: frozenset[str] = frozenset()
    trusted_input_paths: tuple[str | Path, ...] = ()
    candidate_support_dirs: frozenset[str] = frozenset()
    """Unversioned root directories every candidate worktree receives a copy of.

    Agent tool support (for example a profiler's analysis server) is staged in the
    project root but excluded from Git, so a linked worktree would not contain it.
    """


@dataclass(frozen=True, slots=True)
class ProjectRunEffects:
    """Injected effects used while opening project-run resources."""

    git_events: GitTrackerEvents
    log_emit: LogEmitter
    on_log_ready: LogReady
    clock: Clock
    """Seconds since the epoch: names log files and stamps new project and run manifests."""
    git_repository: GitRepositoryFactory
    """Builds the ``GitRepository`` of the project root and of every candidate worktree.

    Production passes ``git_repositories(git)``; tests pass a Fake so a run needs no Git process.
    """
    state_stores: StateStoreFactory | None = None
    """Opens each run's ``StateStore``; ``None`` uses the project's crash-atomic local one."""
    resume_recovery: Callable[[], None] | None = None
    """Recover product resources after taking the run-host lock, before Git or state recovery."""
    capture_recovery: CaptureExecutionRecovery | None = None
    """Reconcile retained capture identities without opening a replacement workspace session."""


@dataclass(slots=True)
class ProjectRunResources:
    """Own one run's project, Git, logger, and typed-state coordination.

    Construction either returns a fully initialized owner or closes everything
    acquired so far. ``close`` is idempotent and releases resources in reverse
    construction order.
    """

    project: Project
    git: GitTracker
    logger: RunLogger
    state: RunState
    objective_document: Path | None
    _teardown_stack: ReleaseStack
    _request: ProjectRunRequest
    _effects: ProjectRunEffects
    _provisional_ownership: ReleaseStack
    root_edits_on_resume: tuple[str, ...] = ()
    _close: CloseOnce = field(init=False, default_factory=CloseOnce)

    @property
    def resuming(self) -> bool:
        """Whether the operator reopened this run, rather than recovering its writer."""
        return self._request.existing

    def mark_ready(self) -> None:
        """Preserve a provisioned project after all run resources are ready."""
        self._provisional_ownership.pop_all()

    def open_candidate(self, workspace_id: str, revision: str) -> _ProjectWorkspaceResources:
        """Open an isolated candidate worktree owned by the returned resource.

        A leftover directory from a stopped process is replaced by a fresh
        worktree of *revision*.
        """
        self._require_settled_candidate(workspace_id)
        return self._open_candidate(workspace_id, revision, reattach=False)

    def reattach_candidate(
        self, workspace_id: str, revision: str
    ) -> _ProjectWorkspaceResources | None:
        """Reopen an existing candidate worktree with its content, or return ``None``."""
        self._require_settled_candidate(workspace_id)
        workspace = self.project.state.candidate_worktree_directory(
            self._request.run_id, workspace_id
        )
        if not workspace.exists():
            return None
        return self._open_candidate(workspace_id, revision, reattach=True)

    def leftover_candidates(self) -> tuple[str, ...]:
        """The workspace IDs of this run's candidate worktrees on disk, in name order."""
        worktrees = self.project.state.candidate_worktrees_directory(self._request.run_id)
        if not worktrees.is_dir():
            return ()
        return tuple(
            sorted(entry.name for entry in worktrees.iterdir() if (entry / "workspace").exists())
        )

    def record_capture_intent(self, workspace_id: str, execution: CaptureExecutionIdentity) -> None:
        """Durably record the external identity before a capture may be submitted."""
        namespace = self.state.local("runtime")
        relative = f"{_UNSETTLED_CANDIDATES}/{workspace_id}"
        if namespace.load_optional(relative, CaptureIntent) is not None:
            raise ProjectRunUnsettledCandidateError(workspace_id)
        namespace.save(
            relative,
            CaptureIntent(workspace_id=workspace_id, execution=execution),
        )

    def clear_capture_intent(self, workspace_id: str) -> None:
        """Remove a capture intent only after its execution is confirmed settled."""
        self.state.local("runtime").delete(f"{_UNSETTLED_CANDIDATES}/{workspace_id}")

    def capture_intents(self) -> tuple[CaptureIntent, ...]:
        """Load all capture intents, rejecting malformed durable state."""
        return _load_capture_intents(self.state.local("runtime"))

    def reconcile_capture_intents(self) -> frozenset[str]:
        """Settle this run's durable captures through its startup recovery owner."""
        return _reconcile_capture_intents(
            self.project,
            self._request.run_id,
            self.state.local("runtime"),
            self._effects.capture_recovery,
        )

    def unsettled_candidates(self) -> frozenset[str]:
        """Return candidates whose remote command may still be using their files."""
        return frozenset(
            intent.workspace_id
            for intent in self.capture_intents()
            if intent.workspace_id != _ROOT_CAPTURE_WORKSPACE
        )

    def _require_settled_candidate(self, workspace_id: str) -> None:
        if workspace_id in self.unsettled_candidates():
            raise ProjectRunUnsettledCandidateError(workspace_id)

    def remove_leftover(self, workspace_id: str) -> None:
        """Unregister and delete a candidate worktree that no live resource owns."""
        self._require_settled_candidate(workspace_id)
        self.git.remove_worktree(
            self.project.state.candidate_worktree_directory(self._request.run_id, workspace_id)
        )

    def _open_candidate(
        self, workspace_id: str, revision: str, *, reattach: bool
    ) -> _ProjectWorkspaceResources:
        workspace = self.project.state.candidate_worktree_directory(
            self._request.run_id, workspace_id
        )
        log_dir = self.state.local("runtime").external_directory(f"workspaces/{workspace_id}/logs")
        if workspace.exists() and not reattach:
            # A stable (member-keyed) workspace ID reuses one path across
            # processes. The runtime holds at most one live candidate per ID,
            # so a directory already here was left by a process that stopped
            # before discarding it.
            self.git.remove_worktree(workspace)
        teardown_stack = ReleaseStack()
        try:
            teardown_stack.callback(self.git.remove_worktree, workspace)
            if not reattach:
                self.git.add_worktree(workspace, revision)
            if not reattach:
                copy_support_directories(
                    self._request.project_root, workspace, self._request.candidate_support_dirs
                )
            logger = RunLogger(
                log_dir, clock=self._effects.clock, tee_stderr=False, emit=self._effects.log_emit
            )
            teardown_stack.callback(logger.close)
            # The agent can rewrite the workspace's ``.git``; host Git follows the
            # parent repository's registration instead.
            repository = self._effects.git_repository(workspace, self._effects.git_events)
            repository.pin(self.git.linked_worktree_location(workspace))
            git = GitTracker(
                workspace,
                run_id=self._request.run_id,
                events=self._effects.git_events,
                repository=repository,
                excluded_dirs=self._request.excluded_dirs,
                excluded_files=self._request.excluded_files,
                trusted_input_paths=self._request.trusted_input_paths,
            )
            if self.git.trusted_input_baseline is not None:
                git.configure_trusted_input_baseline(self.git.trusted_input_baseline)
            return _ProjectWorkspaceResources(
                workspace_id=workspace_id,
                project_root=workspace,
                git=git,
                logger=logger,
                parent_git=self.git,
                state=self.state,
                teardown_stack=teardown_stack,
            )
        except BaseException as construction_error:
            _close_after_construction_failure(teardown_stack, construction_error)
            raise

    def close(self) -> None:
        """Release owned resources in reverse construction order; a failed step runs again next call."""
        self._close.run(self._teardown_stack.close)

    def __enter__(self) -> ProjectRunResources:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


@dataclass(slots=True)
class _ProjectWorkspaceResources:
    """Own one candidate's linked worktree, Git tracker, and logger."""

    workspace_id: str
    project_root: Path
    git: GitTracker
    logger: RunLogger
    parent_git: GitTracker
    state: RunState
    teardown_stack: ReleaseStack
    _close: CloseOnce = field(init=False, default_factory=CloseOnce)

    def retain(self, revision: str, reference: str | None = None) -> None:
        """Keep a candidate revision reachable after its worktree closes."""
        self.parent_git.retain_candidate(reference or self.workspace_id, revision)

    def close(self) -> None:
        """Release the logger and linked worktree; a failed step runs again on the next call."""
        self._close.run(self.teardown_stack.close)

    def detach(self) -> None:
        """Release the logger and keep the linked worktree, its content included, on disk.

        A host that stops while core still holds this candidate leaves it where
        ``reattach_candidate`` reopens it; only ``close`` (a discard) removes it.
        """
        logger = ReleaseStack()
        logger.callback(self.logger.close)
        self._close.run(logger.close)

    def __enter__(self) -> _ProjectWorkspaceResources:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def open_project_run_resources(
    request: ProjectRunRequest,
    *,
    effects: ProjectRunEffects,
    buffered_logs: Iterable[str] = (),
    resolve_resume: ResumeResolver,
) -> ProjectRunResources:
    """Open a fully initialized project run or unwind partial construction.

    ``resolve_resume`` is the sole policy seam. It validates product-owned
    manifest facts and decides whether the requested orchestration descriptor
    may replace the recorded descriptor. Runtime applies that decision and
    owns all persistence, Git snapshots, recovery, logging, and cleanup.
    """
    teardown_stack = ReleaseStack()
    try:
        return _assemble_project_run_resources(
            request,
            effects=effects,
            buffered_logs=buffered_logs,
            resolve_resume=resolve_resume,
            teardown_stack=teardown_stack,
        )
    except BaseException as construction_error:
        _close_after_construction_failure(teardown_stack, construction_error)
        raise


def _assemble_project_run_resources(
    request: ProjectRunRequest,
    *,
    effects: ProjectRunEffects,
    buffered_logs: Iterable[str],
    resolve_resume: ResumeResolver,
    teardown_stack: ReleaseStack,
) -> ProjectRunResources:
    provisional_ownership = ReleaseStack()
    teardown_stack.callback(provisional_ownership.close)
    if request.provisional_project is not None:
        provisional_ownership.callback(request.provisional_project.discard_project)

    with boot_trace.span("project_open"):
        project = Project.open(request.project_root, state_stores=effects.state_stores)
        project_state = project.state
        teardown_stack.enter_context(project_state.exclusive_run_host(request.run_id))
        if request.existing and effects.resume_recovery is not None:
            effects.resume_recovery()
        if request.existing:
            _reconcile_capture_intents(
                project,
                request.run_id,
                project_state.local_namespace(request.run_id, "runtime"),
                effects.capture_recovery,
            )
        log_dir = project_state.log_directory(request.run_id)
        log_dir.mkdir(parents=True, exist_ok=True)

    with boot_trace.span("log_bootstrap"):
        effects.on_log_ready(log_dir)
        logger = RunLogger(log_dir, clock=effects.clock, emit=effects.log_emit)
        teardown_stack.callback(logger.close)
        for message in buffered_logs:
            logger.lprint(message)

    with boot_trace.span("git_tracker_init"):
        git = GitTracker(
            project.root,
            run_id=request.run_id,
            events=effects.git_events,
            repository=effects.git_repository(project.root, effects.git_events),
            excluded_dirs=request.excluded_dirs,
            excluded_files=request.excluded_files,
            trusted_input_paths=request.trusted_input_paths,
        )
        git.init(existing=request.existing)

    with boot_trace.span("project_state_resume"):
        if request.existing:
            project_state.load_project()
            run_manifest = project_state.load_run(request.run_id)
            _validate_resume_identity(request, run_manifest, git)
            run_manifest = _migrate_run_environment(
                request.run_id, project, git, run_manifest, log=logger.lprint
            )
            decision = resolve_resume(run_manifest)
            _apply_resume_decision(request.run_id, project, git, decision)
            project_state.set_current_run(request.run_id)
        else:
            created_at = utc_datetime(effects.clock)
            project_state.create_project(project.root.name, now=created_at)
            trusted_input_baseline = git.trusted_input_baseline
            if trusted_input_baseline is None:
                raise ProjectRunBaselineMissingError
            run_manifest = project_state.new_run_manifest(
                request.display_name,
                task_name=request.task_name,
                run_id=request.run_id,
                branch=git.project_branch,
                vibesys_version=request.framework_version,
                run_environment=request.run_environment,
                execution=request.execution,
                orchestration=request.orchestration,
                trusted_input_baseline=trusted_input_baseline,
                now=created_at,
            )
            project_state.create_run(run_manifest)
            git.snapshot_with_framework_metadata(
                f"vibesys: initialize run {request.run_id}",
                project_state.initialization_snapshot(request.run_id),
            )

    state = RunState(project, git, request.run_id)
    objective_document = _record_effective_objective(request, state, git)
    return ProjectRunResources(
        project,
        git,
        logger,
        state,
        objective_document,
        teardown_stack,
        request,
        effects,
        provisional_ownership,
        root_edits_on_resume=tuple(git.pending_changes()) if request.existing else (),
    )


def _load_capture_intents(namespace: StateNamespace) -> tuple[CaptureIntent, ...]:
    """Load the strictly typed intent set from one runtime state namespace."""
    intents: list[CaptureIntent] = []
    for entry in namespace.entries(_UNSETTLED_CANDIDATES):
        intent = namespace.load(f"{_UNSETTLED_CANDIDATES}/{entry}", CaptureIntent)
        if intent.workspace_id != entry:
            message = f"capture intent entry {entry!r} names workspace {intent.workspace_id!r}"
            raise ProjectRunError(message)
        intents.append(intent)
    return tuple(intents)


def _reconcile_capture_intents(
    project: Project,
    run_id: str,
    namespace: StateNamespace,
    recovery: CaptureExecutionRecovery | None,
) -> frozenset[str]:
    """Reconcile durable ownership, retaining every unvalidated or unknown identity."""
    unresolved: set[str] = set()
    for intent in _load_capture_intents(namespace):
        workspace = (
            project.root
            if intent.workspace_id == _ROOT_CAPTURE_WORKSPACE
            else project.state.candidate_worktree_directory(run_id, intent.workspace_id)
        )
        try:
            settled = recovery is not None and recovery.reconcile(workspace, intent.execution)
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-213425 [BLE001]; startup recovery must preserve durable ownership after any external-boundary or host-cancellation failure.
            settled = False
        if settled is True:
            namespace.delete(f"{_UNSETTLED_CANDIDATES}/{intent.workspace_id}")
        else:
            unresolved.add(intent.workspace_id)
    return frozenset(unresolved)


def _record_effective_objective(
    request: ProjectRunRequest,
    state: RunState,
    git: GitTracker,
) -> Path | None:
    objective = request.objective
    if objective is None:
        return None
    runtime_state = state.portable("runtime")
    destination = runtime_state.external_directory() / "effective-objective.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = materialize_objective_document(
        objective,
        workspace=request.project_root,
        authored_document=None,
        destination=destination,
    )
    git.snapshot_framework_state(
        "vibesys: record effective objective",
        runtime_state.snapshot(),
    )
    return document


def _validate_resume_identity(
    request: ProjectRunRequest,
    manifest: OrchestrationRunManifest,
    git: GitTracker,
) -> None:
    baseline = git.trusted_input_baseline
    if baseline is None:
        git.configure_trusted_input_baseline(manifest.trusted_input_baseline)
    elif baseline != manifest.trusted_input_baseline:
        raise ProjectRunMismatchError(
            request.run_id,
            ProjectRunMismatchKind.TRUSTED_INPUT_BASELINE,
            manifest.trusted_input_baseline,
            baseline,
        )
    if manifest.branch != git.project_branch:
        raise ProjectRunMismatchError(
            request.run_id,
            ProjectRunMismatchKind.BRANCH,
            manifest.branch,
            git.project_branch,
        )
    if manifest.task_name != request.task_name:
        raise ProjectRunMismatchError(
            request.run_id,
            ProjectRunMismatchKind.TASK,
            manifest.task_name,
            request.task_name,
        )


def _migrate_run_environment(
    run_id: str,
    project: Project,
    git: GitTracker,
    manifest: OrchestrationRunManifest,
    *,
    log: Callable[[str], None],
) -> OrchestrationRunManifest:
    """Persist the environment a resumed run continues in, logging a migration."""
    migrated = migrate_recorded_run_environment(manifest.run_environment)
    if migrated == manifest.run_environment:
        return manifest
    log(
        f"[resume] run {run_id} was recorded with the {manifest.run_environment.name!r} "
        f"agent environment; continuing in {migrated.name!r}"
    )
    project.state.update_run_environment(run_id, migrated)
    git.snapshot_framework_metadata_only(
        "vibesys: migrate run environment", project.state.run_manifest_snapshot(run_id)
    )
    return manifest.model_copy(update={"run_environment": migrated})


def _apply_resume_decision(
    run_id: str,
    project: Project,
    git: GitTracker,
    decision: OrchestrationResumeDecision,
) -> None:
    if decision.descriptor is None:
        return
    if decision.requires_clean_workspace:
        pending = git.baseline_changes()
        if pending:
            raise ProjectRunDirtyResumeError(pending)
    project.state.update_run_orchestration(run_id, decision.descriptor)
    snapshot = project.state.run_manifest_snapshot(run_id)
    if decision.requires_clean_workspace:
        git.snapshot_with_framework_metadata("vibesys: update run orchestration", snapshot)
    else:
        git.snapshot_framework_metadata_only("vibesys: migrate run orchestration", snapshot)


def _close_after_construction_failure(
    teardown_stack: ReleaseStack,
    construction_error: BaseException,
) -> None:
    try:
        teardown_stack.close()
    except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-836002 [BLE001]; cleanup must annotate the original construction failure even if teardown raises a BaseException.
        construction_error.add_note(
            "Additional error while cleaning up partial project-run construction: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
