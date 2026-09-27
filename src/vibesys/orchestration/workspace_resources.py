"""Product composition for runtime-owned workspace resources."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from vibesys.context import (
    WorkspaceResourceSpec,
    borrow_run_agent_environment,
    create_workspace_resources,
    open_scoped_agent_environment,
)
from vibesys.events import (
    CoreEventType,
    CoreEventWriter,
    FrameworkSource,
    FrameworkWarningData,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionScope,
    CommandExecutionResult,
    WorkspaceEvaluationSpec,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.orchestration.request import RunRequest
    from vs_runtime.api.infrastructure import (
        AgentExecutionConfiguration,
        AgentExecutionEnvironment,
        TrustedAccuracyResult,
        TrustedBenchmarkResult,
        WorkspaceResource,
    )


@dataclass(frozen=True, slots=True)
class _WorkspaceBinding:
    memory_paths: tuple[str, ...]
    events: CoreEventWriter
    agent_environment_opener: Callable[..., AgentExecutionEnvironment] | None


class _WorkspaceResource:
    """Semantic workspace effects over one product resource assembly."""

    def __init__(
        self,
        parent: _RunResources,
        context: _RunResources,
        workspace_id: str | None,
        binding: _WorkspaceBinding,
    ) -> None:
        self._parent = parent
        self.context = context
        self._id = workspace_id
        self._binding = binding
        self._revision = context.git.current_sha()
        self._closed = False

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def path(self) -> Path:
        return self.context.workspace

    @property
    def revision(self) -> str | None:
        if self._id is None:
            return self.context.git.current_sha()
        return self._revision

    @property
    def trusted_input_baseline(self) -> str | None:
        return self.context.git.trusted_input_baseline

    def snapshot(self, label: str) -> str:
        self.context.git.snapshot(label)
        revision = self.context.git.current_sha()
        if revision is None:
            message = "workspace snapshot completed without a Git revision"
            raise RuntimeError(message)
        self._revision = revision
        if self._id is not None:
            self._parent.git.retain_candidate(self._id, revision)
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
            preserve_paths = tuple(dict.fromkeys((*preserve_paths, *self._binding.memory_paths)))
        restored = self.context.git.checkout_tree(
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
            self._binding.events.emit(
                CoreEventType.FRAMEWORK_WARNING,
                data=FrameworkWarningData(
                    summary=(
                        f"could not restore workspace to revision {revision[:8]}; "
                        "will retry on a later round"
                    ),
                    source=FrameworkSource.GIT_TRACKING,
                    source_label="rollback",
                ),
            )
        return restored

    def retain(self, revision: str, reference: str) -> None:
        self._parent.git.retain_candidate(reference, revision)

    def pending_changes(self) -> list[str]:
        return self.context.git.pending_changes()

    def candidate_patch(self, revision: str) -> str:
        return self.context.git.candidate_patch(revision)

    def trusted_input_changes(self) -> list[str]:
        return self.context.git.trusted_input_changes()

    def is_directory(self, path: str) -> bool:
        return (self.context.workspace / path).is_dir()

    def execute(self, command: str, timeout_seconds: int | None) -> CommandExecutionResult:
        return self.context.run_environment_session.sandbox.execute(
            command,
            timeout=timeout_seconds,
        )

    def agent_scope(self) -> AgentExecutionScope:
        return AgentExecutionScope(
            workspace_path=self.context.workspace,
            log_directory=self.context.log_dir,
            open_environment=partial(self._open_agent_environment, root=self._id is None),
            current_log_file=lambda: self.context.run_log_file,
            environment_variables=self.context.device.gpu_env,
        )

    def _open_agent_environment(
        self,
        configuration: AgentExecutionConfiguration,
        *,
        root: bool,
    ) -> AgentExecutionEnvironment:
        resources = self.context
        if resources.run_environment_view.share_agent_session:
            return borrow_run_agent_environment(
                resources,
                mounts=configuration.resources,
                agent_backend=configuration.spec.backend.value,
                cli_provider=configuration.spec.provider,
            )
        if root and self._binding.agent_environment_opener is not None:
            return self._binding.agent_environment_opener(
                mounts=configuration.resources,
                agent_backend=configuration.spec.backend.value,
                cli_provider=configuration.spec.provider,
            )
        return open_scoped_agent_environment(
            resources,
            mounts=configuration.resources,
            agent_backend=configuration.spec.backend.value,
            cli_provider=configuration.spec.provider,
        )

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec:
        plan = self.context.trusted_evaluation_plan
        view = self.context.run_environment_view
        return WorkspaceEvaluationSpec(
            accuracy_command=plan.accuracy_command,
            benchmark_command=plan.benchmark_command,
            benchmark_contract=plan.benchmark_contract,
            deployment_release_env_var=view.deployment_release_env_var,
        )

    async def trusted_accuracy(self, command_override: str | None) -> TrustedAccuracyResult:
        return await self.context.trusted_evaluation.accuracy(command_override=command_override)

    async def trusted_benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
    ) -> TrustedBenchmarkResult:
        return await self.context.trusted_evaluation.benchmark(
            command_override=command_override,
            required_metrics=required_metrics,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.context.close()


def root_workspace_resource(
    resources: _RunResources,
    memory_paths: tuple[str, ...],
    events: CoreEventWriter,
    agent_environment_opener: Callable[..., AgentExecutionEnvironment] | None,
) -> WorkspaceResource:
    """Adapt the already-open product workspace for runtime ownership."""
    return _WorkspaceResource(
        resources,
        resources,
        None,
        _WorkspaceBinding(memory_paths, events, agent_environment_opener),
    )


class CandidateWorkspaceResourceFactory:
    """Bind product inputs needed to open isolated candidate resources."""

    def __init__(
        self,
        resources: _RunResources,
        request: RunRequest,
        memory_paths: tuple[str, ...],
        events: CoreEventWriter,
        agent_environment_opener: Callable[..., AgentExecutionEnvironment] | None,
    ) -> None:
        """Fix the product resource inputs for every candidate."""
        self._resources = resources
        self._request = request
        self._memory_paths = memory_paths
        self._events = events
        self._agent_environment_opener = agent_environment_opener

    def __call__(self, workspace_id: str, revision: str) -> WorkspaceResource:
        """Open one isolated product environment at a retained revision."""
        context = create_workspace_resources(
            self._resources,
            WorkspaceResourceSpec(
                scope_id=workspace_id,
                revision=revision,
                config=self._request.config,
                agent_backend=self._request.agent_backend,
                cli_provider=self._request.cli_provider,
            ),
        )
        return _WorkspaceResource(
            self._resources,
            context,
            workspace_id,
            _WorkspaceBinding(
                self._memory_paths,
                self._events,
                self._agent_environment_opener,
            ),
        )
