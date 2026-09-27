"""Product composition for the runtime-owned orchestration host."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.composition import AgentToolContext
from vibesys.context import _StateBinding, open_run_resources
from vibesys.orchestration.agents import _Agents, _AgentToolResolver
from vibesys.orchestration.commands import _Commands
from vibesys.orchestration.control import _RunControl
from vibesys.orchestration.gates import _EvaluationAdapter
from vibesys.orchestration.skills import _Skills
from vibesys.orchestration.state import _StateCommitObserver
from vibesys.orchestration.workspace_resources import WorkspaceResourceProvider
from vs_agent.api import AgentSessionState, DurableSessionStore
from vs_runtime.api import ProfileExecution, RunFacts, WorkspaceSourceFact
from vs_runtime.api.infrastructure import (
    BlockingOperations,
    RunHostComponents,
    create_state,
    create_workspaces,
    open_run_host,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from pydantic import BaseModel

    from vibesys.context import _RunResources
    from vibesys.orchestration.contracts import OrchestrationProjector
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import OrchestrationPlugin, RunHost, Workspace
    from vs_sandbox.api import ComputeBackendImpl


@dataclass(frozen=True, slots=True)
class _ProductHostFactory:
    request: RunRequest
    integration: LocalRunIntegration
    open_agent_environment: Callable[..., AgentEnvironment] | None
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
        agents: _Agents | None = None

        async def close_sessions(workspace: Workspace) -> None:
            if agents is None:
                raise _AgentSessionsNotPreparedError
            await agents.close_workspace(workspace)

        workspaces = create_workspaces(
            WorkspaceResourceProvider(
                resources,
                self.request,
                self.plugin.memory_paths,
                self.integration.events,
                close_sessions,
            )
        )
        commands = _Commands(workspaces, blocking)
        skills = _Skills(tuple(resources.skill_source_paths), blocking)
        control = _RunControl(self.integration, debug=self.request.debug)
        state = create_state(
            state_model,
            resources.round_transaction_coordinator,
            workspaces,
            (
                _StateCommitObserver(
                    resources.publish_committed_state,
                    resources.run_id,
                    self.integration.events,
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
            workspaces,
            self.integration.events,
            commands,
        )
        agents = _Agents(
            self.request,
            workspaces,
            session_store,
            self.integration.events,
            resources.lprint,
            AgentToolContext(resources.profiler_kind),
            self.plugin.agents,
            self.agent_tool_bindings,
            control=self.integration.control,
            lifecycle_events=self.integration.agent_execution_event,
            open_agent_environment=self.open_agent_environment,
            client_factory=self.agent_client_factory,
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


class _AgentSessionsNotPreparedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("agent sessions are not prepared")


@asynccontextmanager
async def open_product_run_host(  # noqa: PLR0913  # lint-waiver: LW-948023 [PLR0913]; independent product effects remain explicit at the sole wiring boundary.
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    plugin: OrchestrationPlugin,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
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
