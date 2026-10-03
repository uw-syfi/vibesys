"""Private owner for one project run's durable resources and Git history."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from vs_project.api import (
    GitTracker,
    GitTrackerEvents,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
    RunLogger,
)
from vs_runtime import _boot_trace as boot_trace
from vs_runtime._checkpoint import (
    MultiSlotRoundTransactionCoordinator,
    RoundRecoveryOutcome,
)
from vs_runtime._objective_document import materialize_objective_document
from vs_runtime._run_state import RunState
from vs_runtime.contracts import OrchestrationResumeDecision

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime._project_materialization import ProjectMaterializer

type LogEmitter = Callable[[str, TextIO], None]
type LogReady = Callable[[Path], None]
type ResumeResolver = Callable[[OrchestrationRunManifest], OrchestrationResumeDecision]


class ProjectRunMismatchKind(StrEnum):
    """Persisted facts that must agree before a run can resume."""

    TRUSTED_INPUT_BASELINE = "trusted_input_baseline"
    BRANCH = "branch"
    TASK = "task"


class ProjectRunError(RuntimeError):
    """Base class for rejected project-run resource construction."""


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
class ProjectStateDeclaration:
    """One plugin-owned aggregate state model made durable by the runtime."""

    namespace: str
    model: type[BaseModel]


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
    state: ProjectStateDeclaration | None = None
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
    round_transaction_coordinator: MultiSlotRoundTransactionCoordinator | None
    _teardown_stack: ExitStack
    _request: ProjectRunRequest
    _effects: ProjectRunEffects
    _provisional_ownership: ExitStack
    _closed: bool = field(init=False, default=False)

    def mark_ready(self) -> None:
        """Preserve a provisioned project after all run resources are ready."""
        self._provisional_ownership.pop_all()

    def open_candidate(self, workspace_id: str, revision: str) -> _ProjectWorkspaceResources:
        """Open an isolated candidate worktree owned by the returned resource."""
        workspace = self.project.state.candidate_worktree_directory(
            self._request.run_id, workspace_id
        )
        log_dir = self.state.local("runtime").external_directory(f"workspaces/{workspace_id}/logs")
        if workspace.exists():
            # A stable (member-keyed) workspace ID reuses one path across
            # processes. The runtime holds at most one live candidate per ID,
            # so a directory already here was left by a process that stopped
            # before discarding it.
            self.git.remove_worktree(workspace)
        teardown_stack = ExitStack()
        try:
            teardown_stack.callback(self.git.remove_worktree, workspace)
            self.git.add_worktree(workspace, revision)
            for name in sorted(self._request.candidate_support_dirs):
                source = self._request.project_root / name
                if source.is_dir():
                    shutil.copytree(
                        source,
                        workspace / name,
                        symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__"),
                    )
            logger = RunLogger(log_dir, tee_stderr=False, emit=self._effects.log_emit)
            teardown_stack.callback(logger.close)
            git = GitTracker(
                workspace,
                run_id=self._request.run_id,
                events=self._effects.git_events,
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
        """Release owned resources once, in reverse construction order."""
        if self._closed:
            return
        self._closed = True
        self._teardown_stack.close()

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
    teardown_stack: ExitStack
    _closed: bool = field(init=False, default=False)

    @property
    def objective_document(self) -> Path:
        """Return the candidate equivalent of the run's effective objective."""
        return self.state.portable("runtime").equivalent_external_file(
            self.project_root, "effective-objective.md"
        )

    def retain(self, revision: str, reference: str | None = None) -> None:
        """Keep a candidate revision reachable after its worktree closes."""
        self.parent_git.retain_candidate(reference or self.workspace_id, revision)

    def close(self) -> None:
        """Release the logger and linked worktree exactly once."""
        if self._closed:
            return
        self._closed = True
        self.teardown_stack.close()

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
    teardown_stack = ExitStack()
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
    teardown_stack: ExitStack,
) -> ProjectRunResources:
    provisional_ownership = ExitStack()
    teardown_stack.callback(provisional_ownership.close)
    if request.provisional_project is not None:
        provisional_ownership.callback(request.provisional_project.discard_project)

    with boot_trace.span("project_open"):
        project = Project.open(request.project_root)
        project_state = project.state
        log_dir = project_state.log_directory(request.run_id)
        log_dir.mkdir(parents=True, exist_ok=True)

    with boot_trace.span("log_bootstrap"):
        effects.on_log_ready(log_dir)
        logger = RunLogger(log_dir, emit=effects.log_emit)
        teardown_stack.callback(logger.close)
        for message in buffered_logs:
            logger.lprint(message)

    with boot_trace.span("git_tracker_init"):
        git = GitTracker(
            project.root,
            run_id=request.run_id,
            events=effects.git_events,
            excluded_dirs=request.excluded_dirs,
            excluded_files=request.excluded_files,
            trusted_input_paths=request.trusted_input_paths,
        )
        git.init(existing=request.existing)

    round_transaction_coordinator: MultiSlotRoundTransactionCoordinator | None = None
    with boot_trace.span("project_state_resume"):
        if request.existing:
            project_state.load_project()
            run_manifest = project_state.load_run(request.run_id)
            _validate_resume_identity(request, run_manifest, git)
            decision = resolve_resume(run_manifest)
            round_transaction_coordinator = _round_transaction(request, project, git)
            if round_transaction_coordinator is not None:
                recovery = round_transaction_coordinator.recover()
                if recovery is not RoundRecoveryOutcome.NO_TRANSACTION:
                    logger.lprint(f"[project] recovered round transaction: {recovery.value}")
            _apply_resume_decision(request.run_id, project, git, decision)
            project_state.set_current_run(request.run_id)
        else:
            project_state.create_project(project.root.name)
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
            )
            project_state.create_run(run_manifest)
            git.snapshot_with_framework_metadata(
                f"vibesys: initialize run {request.run_id}",
                project_state.initialization_snapshot(request.run_id),
            )

    with boot_trace.span("round_transaction_recovery"):
        if not request.existing:
            round_transaction_coordinator = _round_transaction(request, project, git)

    state = RunState(project, git, request.run_id)
    objective_document = _record_effective_objective(request, state, git)
    return ProjectRunResources(
        project,
        git,
        logger,
        state,
        objective_document,
        round_transaction_coordinator,
        teardown_stack,
        request,
        effects,
        provisional_ownership,
    )


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


def _apply_resume_decision(
    run_id: str,
    project: Project,
    git: GitTracker,
    decision: OrchestrationResumeDecision,
) -> None:
    if decision.descriptor is None:
        return
    if decision.requires_clean_workspace:
        pending = git.pending_changes()
        if pending:
            raise ProjectRunDirtyResumeError(pending)
    project.state.update_run_orchestration(run_id, decision.descriptor)
    snapshot = project.state.run_manifest_snapshot(run_id)
    if decision.requires_clean_workspace:
        git.snapshot_with_framework_metadata("vibesys: update run orchestration", snapshot)
    else:
        git.snapshot_framework_metadata_only("vibesys: migrate run orchestration", snapshot)


def _round_transaction(
    request: ProjectRunRequest,
    project: Project,
    git: GitTracker,
) -> MultiSlotRoundTransactionCoordinator | None:
    if request.state is None:
        return None
    return MultiSlotRoundTransactionCoordinator(
        project,
        git,
        request.run_id,
        namespace=request.state.namespace,
        models={"state.json": request.state.model},
    )


def _close_after_construction_failure(
    teardown_stack: ExitStack,
    construction_error: BaseException,
) -> None:
    try:
        teardown_stack.close()
    except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-836002 [BLE001]; cleanup must annotate the original construction failure even if teardown raises a BaseException.
        construction_error.add_note(
            "Additional error while cleaning up partial project-run construction: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
