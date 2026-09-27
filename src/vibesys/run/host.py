"""Product composition for the runtime-owned orchestration host."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from vibesys.composition import AgentToolContext, agent_spec_from_config
from vibesys.context import _StateBinding, open_run_resources
from vibesys.orchestration.gates import _EvaluationAdapter
from vibesys.orchestration.skills import platform_skill_selection
from vibesys.orchestration.steering import splice_steering
from vibesys.run.agent_events import CoreAgentEventSink
from vs_agent.api import AgentSessionState, DurableSessionStore
from vs_runtime.api import ProfileExecution, RunFacts, WorkspaceSourceFact
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    BlockingOperations,
    RunHostComponents,
    WorkspaceResourceFactory,
    create_model_request_reconciler,
    create_runtime_control,
    create_state,
    create_workspace_runtime,
    open_run_host,
)
from vs_runtime.api.infrastructure_skills import create_installed_skills

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from pydantic import BaseModel

    from vibesys.context import _RunResources
    from vibesys.orchestration.contracts import OrchestrationProjector
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol, ToolServerDescriptor
    from vs_runtime.api import AgentRole, OrchestrationPlugin, RunHost, Workspace
    from vs_runtime.api.infrastructure import (
        AgentExecutionEnvironment,
        WorkspaceRuntime,
    )
    from vs_sandbox.api import ComputeBackendImpl


type _AgentToolResolver = Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]


@dataclass(frozen=True, slots=True)
class _ProductHostFactory:
    request: RunRequest
    integration: LocalRunIntegration
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None
    projector: OrchestrationProjector | None
    agent_client_factory: Callable[..., AgentClientProtocol] | None
    backend_factory: Callable[..., ComputeBackendImpl] | None
    agent_tool_bindings: Mapping[str, _AgentToolResolver] | None
    plugin: OrchestrationPlugin

    def prepare(self) -> RunHostComponents:
        """Open product resources and bind focused runtime capabilities."""
        plugin = self.plugin
        if self.request.orchestration.id != plugin.id:
            message = (
                f"selected orchestration {self.request.orchestration.id!r} does not match "
                f"plugin {plugin.id!r}"
            )
            raise ValueError(message)
        state_namespace = plugin.id if plugin.state is not None else None
        resources = open_run_resources(
            self.request,
            self.integration,
            resume_policy=plugin.resume_policy,
            state_binding=(
                _StateBinding(state_namespace, plugin.state)
                if state_namespace is not None and plugin.state is not None
                else None
            ),
            backend_factory=self.backend_factory,
        )
        try:
            return self._components(resources, state_namespace, plugin.state)
        except BaseException as construction_error:
            try:
                resources.close()
            except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-948022 [BLE001]; product construction preserves its root failure while still reporting resource-cleanup failure.
                construction_error.add_note(
                    "Additional error while cleaning up host construction: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    def _components(
        self,
        resources: _RunResources,
        state_namespace: str | None,
        state_model: type[BaseModel] | None,
    ) -> RunHostComponents:
        blocking = BlockingOperations()
        session_store = DurableSessionStore(
            resources.state.local("agent").slot("sessions.json", AgentSessionState),
            log=resources.logger.lprint,
        )
        workspace_resources = WorkspaceResourceFactory(
            resources.project_resources,
            resources.environment_resources,
            evaluation_plan=resources.trusted_evaluation_plan,
            memory_paths=self.plugin.memory_paths,
            skill_source_dirs=tuple(resources.skill_source_paths),
            skill_selection=platform_skill_selection(resources.backend),
            host_resources=resources.agent_host_resources,
            events=self.integration.workspace_resource_event,
            model_requests=(
                create_model_request_reconciler()
                if resources.run_environment_view.env_kind == "modal"
                else None
            ),
            root_agent_environment_opener=self._root_agent_environment_opener(),
        )
        agent_runtime = self._agent_runtime(
            resources, session_store, workspace_resources=workspace_resources, blocking=blocking
        )
        agents = agent_runtime.agents
        workspaces = agent_runtime.workspaces
        commands = agent_runtime.commands
        skills = create_installed_skills(tuple(resources.skill_source_paths), blocking)
        control = create_runtime_control(self.integration.control, blocking)
        state = create_state(
            state_model,
            resources.round_transaction_coordinator,
            workspaces,
            (
                self.integration.state_commit_observer(
                    resources.run_id,
                    self.projector,
                    state_namespace,
                )
                if state_namespace is not None
                else None
            ),
        )
        evaluation = _EvaluationAdapter(
            resources.run_id,
            self.request,
            agent_runtime,
            self.integration.events,
            resources.lprint,
        )
        return RunHostComponents(
            run_id=resources.run_id,
            facts=_run_facts(self.request, resources),
            agents=agents,
            workspaces=workspaces,
            evaluation=evaluation,
            state=state,
            control=control,
            commands=commands,
            skills=skills,
            log=resources.lprint,
            blocking=blocking,
            resources=resources,
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
        resources: _RunResources,
        session_store: DurableSessionStore,
        *,
        workspace_resources: WorkspaceResourceFactory,
        blocking: BlockingOperations,
    ) -> WorkspaceRuntime:
        """Bind product configuration to the runtime's resource owner."""

        def resolve_configuration(role: AgentRole) -> AgentExecutionConfiguration:
            spec = agent_spec_from_config(
                self.request.config,
                backend=self.request.agent_backend,
                provider=self.request.cli_provider,
            )
            return AgentExecutionConfiguration(
                agent_id=role.id,
                spec=spec,
                reasoning_effort=spec.role_reasoning_efforts.get(role.id, spec.reasoning_effort),
            )

        tool_context = AgentToolContext(resources.profiler_kind.value)
        return create_workspace_runtime(
            self.plugin.agents,
            workspace_resources=workspace_resources,
            resolve_configuration=resolve_configuration,
            session_store=lambda: session_store,
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
            log=resources.lprint,
        )


def _run_facts(request: RunRequest, resources: _RunResources) -> RunFacts:
    bundle = request.input_bundle
    view = resources.run_environment_view
    return RunFacts(
        domain_id=bundle.domain.value,
        objective=request.objective or bundle.objective,
        environment_notes=view.prompt_notes,
        profile_execution=ProfileExecution(view.profile_execution),
        objective_location=view.paths.objective,
        reference_location=resources.ref_name,
        accuracy_command=view.paths.accuracy_command,
        benchmark_command=view.paths.benchmark_command,
        accuracy_configured=bool(view.paths.accuracy_command),
        benchmark_configured=(
            bundle.benchmark_result is not None or bundle.benchmark_result_protocol is not None
        ),
        profiler_id=resources.profiler_kind.value,
        workspace_sources=tuple(
            WorkspaceSourceFact(name=source.name, dest=source.dest)
            for source in bundle.workspace_sources
        ),
    )


@asynccontextmanager
async def open_product_run_host(  # noqa: PLR0913  # lint-waiver: LW-948023 [PLR0913]; independent product effects remain explicit at the sole wiring boundary.
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    plugin: OrchestrationPlugin,
    open_agent_environment: Callable[..., AgentExecutionEnvironment] | None = None,
    projector: OrchestrationProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    agent_tool_bindings: Mapping[str, _AgentToolResolver] | None = None,
) -> AsyncIterator[RunHost]:
    """Open one product-composed host under the reusable runtime lifecycle."""
    factory = _ProductHostFactory(
        request=request,
        integration=integration,
        open_agent_environment=open_agent_environment,
        projector=projector,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        agent_tool_bindings=agent_tool_bindings,
        plugin=plugin,
    )
    async with open_run_host(factory.prepare) as host:
        yield host


__all__ = ["open_product_run_host"]
