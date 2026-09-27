"""Concrete workspace resources composed from runtime-owned project and environment owners."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from vs_runtime._agent_execution import (
    AgentExecutionConfiguration,
    AgentExecutionScope,
    open_agent_execution_environment,
)
from vs_runtime._trusted_evaluation import (
    TrustedEvaluationPlan,
    create_trusted_evaluation_executor,
)
from vs_runtime._workspace_runtime import CommandExecutionResult, WorkspaceEvaluationSpec

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
        TrustedEvaluationExecutor,
    )
    from vs_sandbox.api import HostResource


@dataclass(frozen=True, slots=True)
class WorkspaceRestoreFailed:
    """Observation emitted when a best-effort workspace restore fails."""

    revision: str


type WorkspaceResourceEvent = WorkspaceRestoreFailed
type WorkspaceResourceEventSink = Callable[[WorkspaceResourceEvent], object]


class ModelRequestReconciler(Protocol):
    """Optional candidate model-resource reconciler used by trusted evaluation."""

    def reconcile(
        self,
        workspace: Path,
        *,
        log: Callable[[str], object] = print,
    ) -> tuple[str, ...]: ...


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
        ownership = ExitStack()
        try:
            project = self._project.open_candidate(workspace_id, revision)
            ownership.callback(project.close)
            base = self._environment.request
            environment = self._environment.open_workspace(
                replace(
                    base,
                    log_dir=project.logger.log_dir,
                    workspace=project.project_root,
                    ref_dir=None,
                    objective_document=(
                        project.objective_document if base.objective is not None else None
                    ),
                    git_history_root=self._project.git.history_root,
                    log=project.logger.lprint,
                )
            )
            ownership.callback(environment.close)
            return self._resource(
                project,
                environment,
                workspace_id=workspace_id,
                ownership=ownership,
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
        ownership: ExitStack | None = None,
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
            host_resources=self._host_resources,
            events=self._events,
            ownership=ownership,
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
        ownership: ExitStack | None,
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
        self._root_agent_environment_opener = root_agent_environment_opener
        self._closed = False

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def path(self) -> Path:
        return self._environment.request.workspace

    @property
    def revision(self) -> str | None:
        if self._id is None:
            return self._project.git.current_sha()
        return self._revision

    @property
    def trusted_input_baseline(self) -> str | None:
        return self._project.git.trusted_input_baseline

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
    ) -> bool:
        if preserve_memory:
            preserve_paths = tuple(dict.fromkeys((*preserve_paths, *self._memory_paths)))
        restored = self._project.git.checkout_tree(
            revision,
            clean=clean,
            preserve_paths=preserve_paths,
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

    def pending_changes(self) -> list[str]:
        return self._project.git.pending_changes()

    def candidate_patch(self, revision: str) -> str:
        return self._project.git.candidate_patch(revision)

    def trusted_input_changes(self) -> list[str]:
        return self._project.git.trusted_input_changes()

    def is_directory(self, path: str) -> bool:
        return (self.path / path).is_dir()

    def execute(self, command: str, timeout_seconds: int | None) -> CommandExecutionResult:
        return self._environment.session.sandbox.execute(command, timeout=timeout_seconds)

    def agent_scope(self) -> AgentExecutionScope:
        return AgentExecutionScope(
            workspace_path=self.path,
            log_directory=self._environment.request.log_dir,
            open_environment=self._open_agent_environment,
            current_log_file=self._current_log_file,
            environment_variables=self._environment.device.gpu_env,
        )

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
            accuracy_command=self._evaluation_plan.accuracy_command,
            benchmark_command=self._evaluation_plan.benchmark_command,
            benchmark_contract=self._evaluation_plan.benchmark_contract,
            deployment_release_env_var=view.deployment_release_env_var,
        )

    async def trusted_accuracy(self, command_override: str | None) -> TrustedAccuracyResult:
        return await self._evaluation.accuracy(command_override=command_override)

    async def trusted_benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
    ) -> TrustedBenchmarkResult:
        return await self._evaluation.benchmark(
            command_override=command_override,
            required_metrics=required_metrics,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ownership is not None:
            self._ownership.close()


def _close_after_construction_failure(
    ownership: ExitStack, construction_error: BaseException
) -> None:
    try:
        ownership.close()
    except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-314369 [BLE001]; partial construction must preserve its primary failure while recording teardown failure.
        construction_error.add_note(
            "Additional error while cleaning up workspace resources: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
