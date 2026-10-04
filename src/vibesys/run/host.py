"""Product composition for the runtime-owned orchestration host."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.composition import AgentToolContext, resolve_agent_specs
from vibesys.events import (
    AsyncOperationKind,
    AsyncOperationLifecycleData,
    AsyncOperationState,
    CoreEventType,
    FrameworkSource,
    FrameworkWarningData,
)
from vibesys.orchestration.profiler_agent import RuntimeProfilerTurnProvision
from vibesys.orchestration.skill_selection import platform_skill_selection
from vibesys.run.agent_events import CoreAgentEventSink
from vibesys.run.evaluation import create_evaluation
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vibesys.run.resources import _StateBinding, open_run_resources
from vibesys.run.slurm_evaluation import SlurmSemanticEvaluationExecutor
from vibesys.steering import splice_steering
from vs_agent.api import AgentInvocationState, AgentSessionState, DurableSessionStore
from vs_evaluation.api import (
    ContentDigest,
    EvaluationAgentService,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerLifecycleEvent,
)
from vs_runtime.api import RunCleanupError
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    BlockingOperations,
    RunHostComponents,
    WorkspaceResourceFactory,
    bounded_stop,
    create_model_request_reconciler,
    create_runtime_control,
    create_state,
    create_workspace_runtime,
    open_run_host,
    stop_gated_evaluation,
)
from vs_runtime.api.infrastructure_skills import create_installed_skills
from vs_sandbox.api import HostResource, HostResourceAccess
from vs_sandbox.api.slurm import load_slurm_policy, read_slurm_evaluation_plan
from vs_slurm.api import load_slurm_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from contextlib import ExitStack

    from pydantic import BaseModel

    from vibesys.events import CoreEventWriter
    from vibesys.run.contracts import RunRequest
    from vibesys.run.integration import CommittedStateProjector, LocalRunIntegration
    from vibesys.run.resources import _PreparedRun
    from vs_agent.api import AgentClientProtocol, ToolServerDescriptor
    from vs_evaluation.api import EvaluationLifecycleEvent
    from vs_project.api import StateNamespace
    from vs_runtime.api import (
        AgentRole,
        AgentToolBindingContext,
        Evaluation,
        OrchestrationDescriptor,
        OrchestrationPlugin,
        OrchestrationResumeDecision,
        Run,
        WorkspaceAgentSessions,
        Workspaces,
    )
    from vs_runtime.api.infrastructure import (
        AgentExecutionEnvironment,
        StopTimer,
        WorkspaceRuntime,
    )
    from vs_sandbox.api import ComputeBackendImpl


type _AgentToolResolver = Callable[
    [object, AgentToolBindingContext], tuple[ToolServerDescriptor, ...]
]
_EVALUATION_CLEANUP_FAILURE = "evaluation agent cleanup failed"

STOP_GRACE_S = 60.0
"""Seconds in-flight agent turns get to end on their own after a stop request.

A stop rejects new evaluations and profiler dispatches at once and cancels the
evaluations agents submitted, so a turn's next evaluation tool call tells it
the run is stopping. Ending the turn then takes about one more model round
trip (seconds to a few tens of seconds), and 60 s allows two. After that the
run is cancelled like a repeated signal, which kills the agent processes;
without this bound a turn could run until the agent CLI timeout (3600 s).
"""


@dataclass(frozen=True, slots=True)
class _ProductObservations:
    """Project plugin-authored observations onto VibeSys logs and events."""

    log: Callable[[str], None]
    events: CoreEventWriter

    def note(self, message: str) -> None:
        """Write one informational note to the durable diagnostic log."""
        self.log(message)

    def warning(self, message: str) -> None:
        """Write and publish one non-fatal orchestration warning."""
        self.log(message)
        self.events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(summary=message, source=FrameworkSource.LOOP),
        )


@dataclass(slots=True)
class _ProductHostFactory:
    request: RunRequest
    integration: LocalRunIntegration
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None
    projector: CommittedStateProjector | None
    agent_client_factory: Callable[..., AgentClientProtocol] | None
    backend_factory: Callable[..., ComputeBackendImpl] | None
    agent_tool_bindings: Mapping[str, _AgentToolResolver] | None
    plugin: OrchestrationPlugin
    resume_policy: (
        Callable[
            [OrchestrationDescriptor, OrchestrationDescriptor],
            OrchestrationResumeDecision,
        ]
        | None
    )
    evaluation_backend: SemanticEvaluationBackend | None = field(default=None, init=False)
    evaluation_service: EvaluationAgentService | None = field(default=None, init=False)
    profiler_service: ProfilerAgentService | None = field(default=None, init=False)
    profiler_provision: RuntimeProfilerTurnProvision | None = field(default=None, init=False)

    def prepare(self, ownership: ExitStack) -> RunHostComponents:
        """Open product resources and bind focused runtime capabilities."""
        plugin = self.plugin
        if self.request.orchestration.id != plugin.id:
            message = (
                f"selected orchestration {self.request.orchestration.id!r} does not match "
                f"plugin {plugin.id!r}"
            )
            raise ValueError(message)
        state_namespace = plugin.id if plugin.state is not None else None
        agent_specs = resolve_agent_specs(
            self.request.config,
            plugin.agents,
            backend=self.request.agent_backend,
            provider=self.request.cli_provider,
        )
        resources = open_run_resources(
            self.request,
            self.integration,
            ownership=ownership,
            agent_specs=agent_specs,
            resume_policy=self.resume_policy,
            state_binding=(
                _StateBinding(state_namespace, plugin.state)
                if state_namespace is not None and plugin.state is not None
                else None
            ),
            backend_factory=self.backend_factory,
        )
        return self._components(resources, state_namespace, plugin.state)

    def _components(
        self,
        resources: _PreparedRun,
        state_namespace: str | None,
        state_model: type[BaseModel] | None,
    ) -> RunHostComponents:
        blocking = BlockingOperations()
        project = resources.project_resources
        environment = resources.environment_resources
        logger = project.logger
        run_id = project.state.run_id
        session_store = DurableSessionStore(
            project.state.local("agent").slot("sessions.json", AgentSessionState),
            log=logger.lprint,
        )
        workspace_resources = WorkspaceResourceFactory(
            project,
            environment,
            evaluation_plan=resources.evaluation_plan,
            memory_paths=self.plugin.memory_paths,
            skill_source_dirs=tuple(resources.skill_source_paths),
            skill_selection=platform_skill_selection(resources.backend),
            host_resources=resources.agent_host_resources,
            events=self.integration.workspace_resource_event,
            model_requests=(
                create_model_request_reconciler() if environment.view.env_kind == "modal" else None
            ),
            root_agent_environment_opener=self._root_agent_environment_opener(),
        )
        tool_context = AgentToolContext(
            resources.facts.profiler_id,
            resources.environment_resources.view.profiler_mcp_env,
        )
        agent_runtime = self._agent_runtime(
            resources,
            session_store,
            workspace_resources=workspace_resources,
            blocking=blocking,
            tool_context=tool_context,
        )
        agents = agent_runtime.agents
        workspaces = agent_runtime.workspaces
        commands = agent_runtime.commands
        skills = create_installed_skills(tuple(resources.skill_source_paths), blocking)
        control = create_runtime_control(self.integration.control, blocking)
        state = create_state(
            state_model,
            project.round_transaction_coordinator,
            workspaces,
            (
                self.integration.state_commit_observer(
                    run_id,
                    self.projector,
                    state_namespace,
                )
                if state_namespace is not None
                else None
            ),
        )
        evaluation = create_evaluation(
            run_id,
            self.request,
            agent_runtime,
            self.integration.events,
            logger.lprint,
        )
        evaluation = self._install_evaluation_service(
            resources,
            evaluation,
            agents,
            workspaces,
            tool_context,
        )
        evaluation = stop_gated_evaluation(evaluation, self.integration.control)
        return RunHostComponents(
            run_id=run_id,
            facts=resources.facts,
            agents=agents,
            workspaces=workspaces,
            evaluation=evaluation,
            state=state,
            control=control,
            commands=commands,
            skills=skills,
            observations=_ProductObservations(logger.lprint, self.integration.events),
            blocking=blocking,
        )

    def _root_agent_environment_opener(
        self,
    ) -> Callable[[AgentExecutionConfiguration], AgentExecutionEnvironment] | None:
        opener = self.open_agent_environment
        if opener is None:
            return None

        def open_environment(
            configuration: AgentExecutionConfiguration,
        ) -> AgentExecutionEnvironment:
            return opener(
                mounts=configuration.resources,
                agent_backend=configuration.spec.backend.value,
                cli_provider=configuration.spec.provider,
            )

        return open_environment

    def _agent_runtime(
        self,
        resources: _PreparedRun,
        session_store: DurableSessionStore,
        *,
        workspace_resources: WorkspaceResourceFactory,
        blocking: BlockingOperations,
        tool_context: AgentToolContext,
    ) -> WorkspaceRuntime:
        """Bind product configuration to the runtime's resource owner."""

        def resolve_configuration(role: AgentRole) -> AgentExecutionConfiguration:
            spec = resources.agent_specs[role.id]
            return AgentExecutionConfiguration(
                agent_id=role.id,
                spec=spec,
                resources=(
                    *(
                        resources.profiler_agent_resources
                        if any(tool.id == "profiler" for tool in role.extra_tools)
                        else ()
                    ),
                    *(
                        (
                            HostResource(
                                self.evaluation_service.socket_path,
                                HostResourceAccess.READ_WRITE,
                                "run evaluation service socket",
                            ),
                        )
                        if self.evaluation_service is not None
                        and any(tool.id == "evaluation" for tool in role.extra_tools)
                        else ()
                    ),
                ),
                reasoning_effort=spec.reasoning_effort,
            )

        return create_workspace_runtime(
            self.plugin.agents,
            workspace_resources=workspace_resources,
            resolve_configuration=resolve_configuration,
            session_store=lambda: session_store,
            invocation_store=lambda key: resources.project_resources.state.local("agent").slot(
                f"invocations/{hashlib.sha256(str(key).encode()).hexdigest()}.json",
                AgentInvocationState,
            ),
            control=self.integration.control,
            lifecycle_events=self.integration.agent_execution_event,
            agent_events=CoreAgentEventSink(self.integration.events.record),
            route_message=splice_steering,
            blocking=blocking,
            client_factory=self.agent_client_factory,
            tool_bindings={
                tool_id: partial(resolver, tool_context)
                for tool_id, resolver in dict(self.agent_tool_bindings or {}).items()
            },
            log=resources.project_resources.logger.lprint,
        )

    def _install_evaluation_service(
        self,
        resources: _PreparedRun,
        evaluation: Evaluation,
        agents: WorkspaceAgentSessions,
        workspaces: Workspaces,
        tool_context: AgentToolContext,
    ) -> Evaluation:
        """Compose evaluation tools and trusted-result reuse when declared."""
        if not any(
            tool.id == "evaluation" for role in self.plugin.agents for tool in role.extra_tools
        ):
            return evaluation
        namespace = resources.project_resources.project.state.local_namespace(
            resources.project_resources.state.run_id,
            "evaluation-agent",
        )
        run_id = resources.project_resources.state.run_id

        def digest(value: str) -> ContentDigest:
            return ContentDigest.sha256(value.encode())

        backend = SemanticEvaluationBackend(
            evaluation,
            workspaces,
            namespace,
            SemanticEvaluationIdentity(
                evaluator=digest(repr(resources.evaluation_plan)),
                workload=digest(resources.facts.model_dump_json()),
                environment=digest(repr(resources.environment_resources.view)),
            ),
            executor=self._semantic_executor(resources, workspaces, namespace),
            events=self._evaluation_lifecycle_event,
            plan=resources.evaluation_plan,
            queue_allowance_seconds=self.request.config.evaluation.queue_allowance_seconds,
        )
        socket_suffix = hashlib.sha256(
            f"{resources.project_resources.project.root}:{run_id}".encode()
        ).hexdigest()[:16]
        profiler_service: ProfilerAgentService | None = None
        profiler_role = next(
            (role for role in self.plugin.agents if role.id in {"profiler", "dynamic-profiler"}),
            None,
        )
        if profiler_role is not None and resources.facts.profiler_id != "none":
            provision = RuntimeProfilerTurnProvision(
                profiler_role,
                agents,
                workspaces,
            )
            profiler_service = ProfilerAgentService(
                provision,
                namespace,
                ProfilerAgentServiceHooks(
                    candidate_snapshot=partial(
                        backend.snapshot,
                        label="profiler-agent-dispatch",
                    ),
                    resolve_evidence=backend.resolve_profile_evidence,
                    events=self._profiler_lifecycle_event,
                ),
            )
            self.profiler_provision = provision
            self.profiler_service = profiler_service
        service = EvaluationAgentService(
            backend,
            namespace,
            Path(tempfile.gettempdir()) / f"vse-{socket_suffix}.sock",
            profiler_service,
            stopping=self.integration.control.stop_requested,
        )
        tool_context.install_evaluation(service, backend)
        self.evaluation_backend = backend
        self.evaluation_service = service
        return EvidenceReusingEvaluation(
            evaluation,
            backend,
            run_id=run_id,
            scopes=service,
            profiler=profiler_service,
        )

    @staticmethod
    def _semantic_executor(
        resources: _PreparedRun,
        workspaces: Workspaces,
        namespace: StateNamespace,
    ) -> SlurmSemanticEvaluationExecutor | None:
        """Select the environment-specific semantic execution adapter at composition."""
        if resources.environment_resources.view.env_kind != "slurm":
            return None
        plan = read_slurm_evaluation_plan(
            resources.environment_resources.request.log_dir / "slurm-evaluation-plan.json"
        )
        config = load_slurm_config(plan.config_path)
        policy = load_slurm_policy(plan.config_path)
        return SlurmSemanticEvaluationExecutor(
            config,
            policy,
            plan,
            resources.evaluation_plan,
            workspaces,
            namespace,
            namespace.external_directory() / "slurm-provider-handles",
        )

    def _evaluation_lifecycle_event(self, event: EvaluationLifecycleEvent) -> None:
        """Project provider-neutral evaluation lifecycle onto the core stream."""
        self.integration.events.emit(
            CoreEventType.ASYNC_OPERATION_LIFECYCLE,
            data=AsyncOperationLifecycleData(
                operation_kind=AsyncOperationKind.EVALUATION,
                operation_id=event.handle_id,
                state=AsyncOperationState(event.phase.value),
                revision=event.revision,
                scope_id=event.scope_id,
                current_stage=event.current_stage,
            ),
        )

    def _profiler_lifecycle_event(self, event: ProfilerLifecycleEvent) -> None:
        """Project delegated profiler lifecycle onto the core stream."""
        self.integration.events.emit(
            CoreEventType.ASYNC_OPERATION_LIFECYCLE,
            data=AsyncOperationLifecycleData(
                operation_kind=AsyncOperationKind.PROFILER,
                operation_id=event.operation_id,
                state=AsyncOperationState(event.state.value),
                scope_id=event.scope_id,
            ),
        )

    async def start_evaluation_service(self) -> None:
        """Start run-owned async evaluation resources before policy executes."""
        if self.evaluation_service is not None:
            await self.evaluation_service.start()
        if self.evaluation_backend is not None:
            await self.evaluation_backend.start()

    async def stop_new_work(self) -> None:
        """Cancel the evaluations agents submitted; the service already rejects new ones."""
        if self.evaluation_service is not None:
            await self.evaluation_service.cancel_outstanding()

    async def close_evaluation_service(self) -> None:
        """Release the service before its workspaces and evaluator dependencies."""
        errors: list[BaseException] = []
        errors.extend(
            await close_evaluation_services(self.evaluation_service, self.profiler_service)
        )
        if self.profiler_provision is not None:
            try:
                await self.profiler_provision.close()
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-930074 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
                errors.append(error)
        if self.evaluation_backend is not None:
            try:
                await self.evaluation_backend.close()
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-930075 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
                errors.append(error)
        if errors:
            raise RunCleanupError(_EVALUATION_CLEANUP_FAILURE, tuple(errors))


async def close_evaluation_services(
    evaluation: EvaluationAgentService | None,
    profilers: ProfilerAgentService | None,
) -> list[BaseException]:
    """Settle delegated operations before releasing their evaluation dependency.

    Admission closes first; observation and evidence remain available until
    every profiler operation has acknowledged a terminal outcome.
    """
    shutdown = asyncio.create_task(_close_evaluation_services(evaluation, profilers))
    try:
        return await asyncio.shield(shutdown)
    except asyncio.CancelledError as cancelled:
        while not shutdown.done():
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError:
                continue
        for error in shutdown.result():
            cancelled.add_note(f"evaluation shutdown also failed: {type(error).__name__}: {error}")
        raise


async def _close_evaluation_services(
    evaluation: EvaluationAgentService | None,
    profilers: ProfilerAgentService | None,
) -> list[BaseException]:
    errors: list[BaseException] = []
    if evaluation is not None:
        evaluation.begin_settling()
    if profilers is not None:
        try:
            await profilers.close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-930073 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
            errors.append(error)
    if evaluation is not None:
        try:
            await evaluation.close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-930072 [BLE001]; all independently owned resources must be released during cancellation; narrower catches would skip cleanup, while a wrapper would only move the same boundary.
            errors.append(error)
    return errors


@asynccontextmanager
async def open_product_run_host(  # noqa: PLR0913  # lint-waiver: LW-948023 [PLR0913]; independent product effects remain explicit at the sole wiring boundary.
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    plugin: OrchestrationPlugin,
    resume_policy: (
        Callable[
            [OrchestrationDescriptor, OrchestrationDescriptor],
            OrchestrationResumeDecision,
        ]
        | None
    ) = None,
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None = None,
    projector: CommittedStateProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    agent_tool_bindings: Mapping[str, _AgentToolResolver] | None = None,
    stop_timer: StopTimer = asyncio.sleep,
) -> AsyncIterator[Run]:
    """Open one product-composed host under the reusable runtime lifecycle.

    A stop requested while the caller's block runs is bounded: new evaluation
    work is rejected and agent-submitted evaluations are cancelled at once,
    and if the block is still running `STOP_GRACE_S` later (timed by
    *stop_timer*), its task is cancelled and the block ends in `RunStopped`.
    Teardown then cancels external jobs before releasing the host.
    """
    factory = _ProductHostFactory(
        request=request,
        integration=integration,
        open_agent_environment=open_agent_environment,
        projector=projector,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        agent_tool_bindings=agent_tool_bindings,
        plugin=plugin,
        resume_policy=resume_policy,
    )
    async with open_run_host(factory.prepare) as host:
        try:
            await factory.start_evaluation_service()
            async with bounded_stop(
                integration.control,
                grace_s=STOP_GRACE_S,
                on_stop=factory.stop_new_work,
                timer=stop_timer,
            ):
                yield host.run
        finally:
            await factory.close_evaluation_service()


__all__ = ["STOP_GRACE_S", "close_evaluation_services", "open_product_run_host"]
