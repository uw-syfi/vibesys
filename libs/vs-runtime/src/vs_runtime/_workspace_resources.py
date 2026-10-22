"""Concrete workspace resources composed from runtime-owned project and environment owners."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vs_core.api import AdoptionPublication, CheckoutStatus, CheckoutUpdate
from vs_project.api import GitError, PublicationError, PublishedRevision
from vs_project.api.workspace_tree import EntryKind
from vs_runtime._agent_execution import (
    AgentExecutionConfiguration,
    AgentExecutionScope,
    open_agent_execution_environment,
)
from vs_runtime._run_environment import RunEnvironmentSessionRole
from vs_runtime._trusted_evaluation import (
    TrustedEvaluationPlan,
    create_trusted_evaluation_executor,
)
from vs_runtime._workspace_files import entry_kind, file_fingerprint
from vs_runtime._workspace_receipts import BaselineEntry, BaselineEntryKind, RootBaselineDrift
from vs_runtime._workspace_runtime import CommandExecutionResult, WorkspaceEvaluationSpec
from vs_runtime.contracts import CaptureStopUnconfirmedError, RuntimeContractError
from vs_sandbox.api import (
    CaptureExecutionIdentity,
    CommandStopUnconfirmedError,
    HostResource,
    HostResourceAccess,
)
from vs_sim.api import CloseOnce, ReleaseStack

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import SkillSelection
    from vs_runtime._agent_execution import AgentExecutionEnvironment
    from vs_runtime._project_run import ProjectRunResources, _ProjectWorkspaceResources
    from vs_runtime._run_environment import RunEnvironmentResources
    from vs_runtime._trusted_evaluation import (
        TrustedAccuracyResult,
        TrustedBenchmarkResult,
        TrustedBuildResult,
        TrustedEvaluationExecutor,
    )
    from vs_sandbox.api import CommandRunner
    from vs_sim.api import BlockingRunner, Event, OutputSink, Threads
from vs_sim.api import DISCARD


@dataclass(frozen=True, slots=True)
class WorkspaceRestoreFailed:
    """Observation emitted when a best-effort workspace restore fails."""

    revision: str


@dataclass(frozen=True, slots=True)
class RootBaselineDrifted:
    """An attempt's hold of the exclusive root ended having left a pre-run path changed or gone.

    See ``vs_runtime._workspace_receipts.RootBaselineDrift``, whose fields this mirrors.
    """

    modified: tuple[str, ...]
    removed: tuple[str, ...]


type WorkspaceResourceEvent = WorkspaceRestoreFailed | RootBaselineDrifted
type WorkspaceResourceEventSink = Callable[[WorkspaceResourceEvent], object]


class ModelRequestReconciler(Protocol):
    """Optional candidate model-resource reconciler used by trusted evaluation."""

    def reconcile(
        self,
        workspace: Path,
        *,
        log: Callable[[str], object] = print,
    ) -> tuple[str, ...]: ...


@runtime_checkable
class _CaptureExecutionProvider(Protocol):
    @property
    def capture_execution(self) -> CaptureExecutionIdentity | None: ...


class WorkspaceResourceFactory:
    """Bind one run's infrastructure and construct root or candidate resources."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-314367 [PLR0913]; inputs are independent run-owned mechanisms and policy declarations, while a setup DTO would expose a second mutable configuration surface.
        self,
        project: ProjectRunResources,
        environment: RunEnvironmentResources,
        *,
        evaluation_plan: TrustedEvaluationPlan,
        memory_paths: tuple[str, ...],
        skill_source_dirs: tuple[Path, ...],
        skill_selection: SkillSelection,
        host_resources: tuple[HostResource, ...],
        events: WorkspaceResourceEventSink,
        threads: Threads,
        blocking: BlockingRunner,
        model_requests: ModelRequestReconciler | None = None,
        root_agent_environment_opener: (
            Callable[[AgentExecutionConfiguration], AgentExecutionEnvironment] | None
        ) = None,
    ) -> None:
        self._project = project
        self._environment = environment
        self._evaluation_plan = evaluation_plan
        self._memory_paths = memory_paths
        self._skill_source_dirs = skill_source_dirs
        self._skill_selection = skill_selection
        self._host_resources = host_resources
        self._events = events
        self._model_requests = model_requests
        self._threads = threads
        self._blocking = blocking
        self._root_agent_environment_opener = root_agent_environment_opener

    @property
    def root(self) -> RuntimeWorkspaceResource:
        """Return a non-owning resource view over the already-open root workspace."""
        return self._resource(self._project, self._environment, workspace_id=None)

    @property
    def supports_parallel_candidates(self) -> bool:
        """Return whether the active environment supports candidate concurrency."""
        return self._environment.view.supports_parallel_candidate_evaluation

    def create_candidate(self, workspace_id: str, revision: str) -> RuntimeWorkspaceResource:
        """Open one isolated candidate and unwind every partial acquisition on failure."""
        return self._wrap_candidate(self._project.open_candidate(workspace_id, revision))

    def reattach_candidate(
        self, workspace_id: str, revision: str
    ) -> RuntimeWorkspaceResource | None:
        """Reopen a worktree left on disk by a stopped host, or return ``None``."""
        project = self._project.reattach_candidate(workspace_id, revision)
        return None if project is None else self._wrap_candidate(project)

    def leftover_candidates(self) -> tuple[str, ...]:
        """The workspace IDs of the run's candidate worktrees on disk."""
        return self._project.leftover_candidates()

    def unsettled_candidates(self) -> frozenset[str]:
        """Candidate IDs retained after an unconfirmed command stop."""
        return self._project.unsettled_candidates()

    def record_capture_intent(self, workspace_id: str, execution: CaptureExecutionIdentity) -> None:
        """Durably protect a candidate before external submission."""
        self._project.record_capture_intent(workspace_id, execution)

    def clear_capture_intent(self, workspace_id: str) -> None:
        """Clear a settled candidate capture intent."""
        self._project.clear_capture_intent(workspace_id)

    def reconcile_capture_intents(self) -> frozenset[str]:
        """Retry the run-scoped recovery owner; never use this root session as a proxy."""
        return self._project.reconcile_capture_intents()

    def remove_leftover(self, workspace_id: str) -> None:
        """Remove a candidate worktree no live handle owns; its sandbox closed with its host."""
        self._project.remove_leftover(workspace_id)

    def _wrap_candidate(self, project: _ProjectWorkspaceResources) -> RuntimeWorkspaceResource:
        workspace_id = project.workspace_id
        ownership = ReleaseStack()
        try:
            ownership.callback(project.close)
            base = self._environment.request
            environment = self._environment.open_workspace(
                replace(
                    base,
                    log_dir=project.logger.log_dir,
                    workspace=project.project_root,
                    ref_dir=None,
                    # A candidate session may reach only its own worktree: the run's
                    # other candidates are not its to read or write through a broker.
                    run_owned_roots=(),
                    session_role=RunEnvironmentSessionRole.CHILD,
                    git_history_root=self._project.git.history_root,
                    log=project.logger.lprint,
                )
            )
            ownership.callback(environment.close)
            # A host stop releases the sandbox and the logger but keeps the worktree.
            detachment = ReleaseStack()
            detachment.callback(project.detach)
            detachment.callback(environment.close)
            return self._resource(
                project,
                environment,
                workspace_id=workspace_id,
                ownership=ownership,
                detachment=detachment,
            )
        except BaseException as construction_error:
            _close_after_construction_failure(ownership, construction_error)
            raise

    def _resource(
        self,
        project: ProjectRunResources | _ProjectWorkspaceResources,
        environment: RunEnvironmentResources,
        *,
        workspace_id: str | None,
        ownership: ReleaseStack | None = None,
        detachment: ReleaseStack | None = None,
    ) -> RuntimeWorkspaceResource:
        plan = self._evaluation_plan.model_copy(
            update={
                "accuracy_command": environment.view.paths.accuracy_command,
                "benchmark_command": environment.view.paths.benchmark_command,
                "framework_setup_timeout_seconds": (
                    environment.view.framework_setup_timeout_seconds
                ),
            }
        )
        git = project.git
        evaluation = create_trusted_evaluation_executor(
            plan,
            workspace=environment.request.workspace,
            sandbox=environment.session.sandbox,
            git=git,
            model_requests=self._model_requests,
            threads=self._threads,
            blocking=self._blocking,
        )
        host_resources = (*self._host_resources, *environment.view.agent_host_resources)
        if environment.request.objective is not None:
            objective_path = (
                environment.request.objective_document
                or environment.request.log_dir / "effective-objective.md"
            ).resolve()
            if not objective_path.is_relative_to(environment.request.workspace):
                host_resources = (
                    *host_resources,
                    HostResource(path=objective_path, access=HostResourceAccess.READ_ONLY),
                )
        return RuntimeWorkspaceResource(
            root_project=self._project,
            project=project,
            environment=environment,
            workspace_id=workspace_id,
            evaluation_plan=plan,
            evaluation=evaluation,
            memory_paths=self._memory_paths,
            skill_source_dirs=self._skill_source_dirs,
            skill_selection=self._skill_selection,
            host_resources=host_resources,
            events=self._events,
            ownership=ownership,
            detachment=detachment,
            root_agent_environment_opener=(
                self._root_agent_environment_opener if workspace_id is None else None
            ),
        )


class RuntimeWorkspaceResource:
    """One concrete workspace implementation consumed by RuntimeWorkspaces."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-314368 [PLR0913]; the concrete resource fixes orthogonal owned capabilities once so every operation remains explicit and local.
        self,
        *,
        root_project: ProjectRunResources,
        project: ProjectRunResources | _ProjectWorkspaceResources,
        environment: RunEnvironmentResources,
        workspace_id: str | None,
        evaluation_plan: TrustedEvaluationPlan,
        evaluation: TrustedEvaluationExecutor,
        memory_paths: tuple[str, ...],
        skill_source_dirs: tuple[Path, ...],
        skill_selection: SkillSelection,
        host_resources: tuple[HostResource, ...],
        events: WorkspaceResourceEventSink,
        ownership: ReleaseStack | None,
        detachment: ReleaseStack | None = None,
        root_agent_environment_opener: (
            Callable[[AgentExecutionConfiguration], AgentExecutionEnvironment] | None
        ),
    ) -> None:
        self._root_project = root_project
        self._project = project
        self._environment = environment
        self._id = workspace_id
        self._revision = project.git.current_sha()
        self._evaluation_plan = evaluation_plan
        self._evaluation = evaluation
        self._memory_paths = memory_paths
        self._skill_source_dirs = skill_source_dirs
        self._skill_selection = skill_selection
        self._host_resources = host_resources
        self._events = events
        self._ownership = ownership
        self._detachment = detachment
        self._root_agent_environment_opener = root_agent_environment_opener
        self._close = CloseOnce()

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def path(self) -> Path:
        return self._environment.request.workspace

    @property
    def owns_checkout(self) -> bool:
        """Return whether the runtime created this checkout and may delete ignored files in it.

        This is the one place the policy lives. A candidate worktree is disposable, so
        restore cleans it exactly and verification compares ignored files. The root
        workspace is the user's checkout: ignored files there (virtualenvs, caches,
        build output) are outside a revision's identity and are never deleted or compared.
        """
        return self._id is not None

    @property
    def revision(self) -> str | None:
        if self._id is None:
            return self._project.git.current_sha()
        return self._revision

    @property
    def trusted_input_baseline(self) -> str | None:
        return self._project.git.trusted_input_baseline

    def publish(self, revision: str, message: str) -> AdoptionPublication:
        try:
            published = self._root_project.git.publish_revision(
                revision,
                message,
                preserve_paths=self._memory_paths,
            )
        except (GitError, PublicationError, ValueError) as error:
            raise RuntimeContractError(str(error)) from error
        return AdoptionPublication(
            revision=published.revision,
            commit=published.commit,
            branch=published.branch,
        )

    def update_checkout(self, publication: AdoptionPublication) -> CheckoutUpdate:
        outcome = self._root_project.git.update_checkout(
            PublishedRevision(
                revision=publication.revision,
                commit=publication.commit,
                branch=publication.branch,
            )
        )
        return CheckoutUpdate(status=CheckoutStatus(outcome.value))

    def snapshot(self, label: str) -> str:
        self._project.git.snapshot(label)
        revision = self._project.git.current_sha()
        if revision is None:
            message = "workspace snapshot completed without a Git revision"
            raise RuntimeError(message)
        self._revision = revision
        if self._id is not None:
            self._root_project.git.retain_candidate(self._id, revision)
        return revision

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
        exact_keeping: tuple[str, ...] | None = None,
    ) -> bool:
        if preserve_memory:
            preserve_paths = tuple(dict.fromkeys((*preserve_paths, *self._memory_paths)))
        restored = self._project.git.checkout_tree(
            revision,
            clean=clean,
            clean_ignored=self.owns_checkout or exact_keeping is not None,
            preserve_paths=preserve_paths,
            keep_paths=exact_keeping or (),
        )
        if restored and self._id is not None:
            self._revision = revision
        return restored

    def try_restore(self, revision: str, *, clean: bool) -> bool:
        restored = self.restore(revision, clean=clean)
        if not restored:
            self._events(WorkspaceRestoreFailed(revision))
        return restored

    def retain(self, revision: str, reference: str) -> None:
        self._root_project.git.retain_candidate(reference, revision)

    def has_revision(self, revision: str) -> bool:
        return self._root_project.git.has_revision(revision)

    def matches_revision(
        self, revision: str, *, exact_keeping: tuple[str, ...] | None = None
    ) -> bool:
        """Return whether the workspace tree equals the revision.

        Ignored files count only in a workspace the runtime owns, or when *exact_keeping*
        is given: then the whole tree counts (an exclusive-root attempt owns what it
        leaves behind) except the paths it lists, which were there before the run.
        Preserved memory paths are exempt.
        """
        return self._project.git.matches_tree(
            revision,
            exempt_paths=(*self._memory_paths, *(exact_keeping or ())),
            include_ignored=self.owns_checkout or exact_keeping is not None,
        )

    def unversioned_paths(self) -> tuple[str, ...]:
        """The untracked and ignored paths present now (directories collapsed)."""
        return self._project.git.unversioned_paths()

    def baseline_entries(self, paths: tuple[str, ...]) -> tuple[BaselineEntry, ...]:
        """Fingerprint *paths* (from :meth:`unversioned_paths`) as they are now.

        A path with a trailing ``/`` is a directory and gets no fingerprint (see
        ``BaselineEntry``); any other path is fingerprinted as a file.
        """
        return tuple(
            BaselineEntry(
                path=path,
                fingerprint=None if path.endswith("/") else file_fingerprint(self.path, path),
            )
            for path in paths
        )

    def baseline_drift(self, entries: tuple[BaselineEntry, ...]) -> RootBaselineDrift:
        """Compare the tree now to *entries*, a recorded pre-run baseline.

        A directory entry is removed if no directory is there now; an unfingerprinted
        entry (see ``BaselineEntryKind``) is removed if nothing is there now. A file entry
        is removed if it is no longer a contained regular file, modified if its content
        fingerprint no longer matches. Any drift is reported through this resource's
        event sink.
        """
        modified: list[str] = []
        removed: list[str] = []
        for entry in entries:
            match entry.kind:
                case BaselineEntryKind.DIRECTORY:
                    gone = entry_kind(self.path, entry.path) is not EntryKind.DIRECTORY
                case BaselineEntryKind.UNFINGERPRINTED:
                    gone = entry_kind(self.path, entry.path) is None
                case BaselineEntryKind.FILE:
                    fingerprint = file_fingerprint(self.path, entry.path)
                    gone = fingerprint is None
                    if fingerprint is not None and fingerprint != entry.fingerprint:
                        modified.append(entry.path)
            if gone:
                removed.append(entry.path)
        drift = RootBaselineDrift(modified=tuple(modified), removed=tuple(removed))
        if not drift.is_empty:
            self._events(RootBaselineDrifted(modified=drift.modified, removed=drift.removed))
        return drift

    def find_snapshot(self, label: str) -> str | None:
        """Return the newest commit whose subject is exactly *label*, if any."""
        return self._project.git.find_snapshot(label)

    def pending_changes(self) -> list[str]:
        return self._project.git.pending_changes()

    def candidate_patch(self, revision: str) -> str:
        return self._project.git.candidate_patch(revision)

    def measured_input(self, revision: str) -> str:
        return self._project.git.measured_input(revision, self._evaluation_plan.unread_paths)

    def withhold_unread(self) -> None:
        self._project.git.withhold_unread(self._evaluation_plan.unread_paths)

    def is_retained(self, revision: str) -> bool:
        return self._root_project.git.is_retained(revision)

    def trusted_input_changes(self) -> list[str]:
        return self._project.git.trusted_input_changes()

    def is_directory(self, path: str) -> bool:
        return entry_kind(self.path, path) is EntryKind.DIRECTORY

    def execute(self, command: str, timeout_seconds: int | None) -> CommandExecutionResult:
        return self._environment.session.sandbox.execute(
            command, timeout=timeout_seconds, output=DISCARD
        )

    def execute_capture(
        self,
        command: str,
        timeout_seconds: int | None,
        cancel: Event,
    ) -> CommandExecutionResult:
        """Execute through the environment's role-specific capture runner."""
        try:
            return self._environment.session.capture_commands.execute(
                command, timeout=timeout_seconds, cancel=cancel, output=DISCARD
            )
        except CommandStopUnconfirmedError as cause:
            raise CaptureStopUnconfirmedError from cause

    @property
    def capture_execution(self) -> CaptureExecutionIdentity | None:
        """Stable external identity when capture can be recovered after restart."""
        session = self._environment.session
        if not isinstance(session, _CaptureExecutionProvider):
            return None
        return session.capture_execution

    def agent_sandbox(self) -> CommandRunner | None:
        environment = self._environment
        return environment.session.sandbox if environment.view.cli_sandboxed else None

    def agent_scope(self) -> AgentExecutionScope:
        return AgentExecutionScope(
            workspace_path=self.path,
            log_directory=self._environment.request.log_dir,
            open_environment=self._open_agent_environment,
            current_log_file=self._current_log_file,
            environment_variables=self._agent_environment_variables,
            agent_homes_directory=self._environment.request.agent_homes_dir,
        )

    def _agent_environment_variables(self) -> dict[str, str]:
        """The device pin the run selected for its agents."""
        return dict(self._environment.device.gpu_env())

    def _open_agent_environment(
        self, configuration: AgentExecutionConfiguration
    ) -> AgentExecutionEnvironment:
        if (
            not self._environment.view.share_agent_session
            and self._root_agent_environment_opener is not None
        ):
            return self._root_agent_environment_opener(configuration)
        return open_agent_execution_environment(
            self._environment.request,
            self._environment.session,
            share_session=self._environment.view.share_agent_session,
            skill_source_dirs=self._skill_source_dirs,
            skill_selection=self._skill_selection,
            host_resources=self._host_resources,
            mounts=configuration.resources,
            agent_backend=configuration.spec.backend.value,
            cli_provider=configuration.spec.provider,
            open_session=self._environment.open_session,
        )

    def _current_log_file(self) -> TextIO:
        return self._project.logger.writer

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec:
        view = self._environment.view
        return WorkspaceEvaluationSpec(
            build=self._evaluation_plan.build,
            accuracy_command=self._evaluation_plan.accuracy_command,
            benchmark_command=self._evaluation_plan.benchmark_command,
            benchmark_contract=self._evaluation_plan.benchmark_contract,
            deployment_release_env_var=view.deployment_release_env_var,
        )

    async def candidate_build(self, output: OutputSink) -> TrustedBuildResult:
        return await self._evaluation.build(confinement=self.agent_sandbox(), output=output)

    async def trusted_accuracy(
        self, command_override: str | None, output: OutputSink
    ) -> TrustedAccuracyResult:
        return await self._evaluation.accuracy(command_override=command_override, output=output)

    async def trusted_benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
        output: OutputSink,
    ) -> TrustedBenchmarkResult:
        return await self._evaluation.benchmark(
            command_override=command_override,
            required_metrics=required_metrics,
            output=output,
        )

    def close(self) -> None:
        """Release what this workspace owns; a step that failed runs again on the next call."""
        if self._ownership is not None:
            self._close.run(self._ownership.close)

    def detach(self) -> None:
        """Release this host's live handles and keep the content on disk for a later host.

        A candidate's sandbox and logger close and its worktree stays, so the next host's
        ``reattach_candidate`` reopens it with the work in it. The root owns nothing here.
        """
        if self._detachment is not None:
            self._close.run(self._detachment.close)


def _close_after_construction_failure(
    ownership: ReleaseStack, construction_error: BaseException
) -> None:
    try:
        ownership.close()
    except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-314369 [BLE001]; partial construction must preserve its primary failure while recording teardown failure.
        construction_error.add_note(
            "Additional error while cleaning up workspace resources: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
