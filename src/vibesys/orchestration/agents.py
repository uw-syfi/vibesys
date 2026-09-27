"""VibeSys composition for runtime-owned explicit agent sessions."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vibesys.composition import agent_spec_from_config
from vibesys.orchestration.steering import splice_steering
from vibesys.run.agent_events import CoreAgentEventSink
from vs_agent.api import build_agent_client
from vs_runtime.api import AgentRole, Workspace
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionLifecycleSink,
    CandidateWorkspaceResourceFactory,
    WorkspaceResource,
    WorkspaceRuntime,
    create_workspace_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.events import CoreEventWriter
    from vibesys.orchestration.request import RunRequest
    from vs_agent.api import AgentClientProtocol, DurableSessionStore, ToolServerDescriptor
    from vs_runtime.api.infrastructure import BlockingOperations, RunControlChannel

type _AgentToolResolver = Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]


def create_agents_and_workspaces(  # noqa: PLR0913  # lint-waiver: LW-837215 [PLR0913]; the product composition root binds independent execution and workspace effects once.
    request: RunRequest,
    session_store: DurableSessionStore,
    events: CoreEventWriter,
    log: Callable[[str], None],
    tool_context: object,
    roles: tuple[AgentRole, ...],
    tool_bindings: Mapping[str, _AgentToolResolver] | None,
    *,
    root_resource: WorkspaceResource,
    supports_parallel_candidates: bool,
    create_candidate_resource: CandidateWorkspaceResourceFactory,
    control: RunControlChannel,
    lifecycle_events: AgentExecutionLifecycleSink,
    blocking: BlockingOperations,
    client_factory: Callable[..., AgentClientProtocol] | None,
) -> WorkspaceRuntime:
    """Bind VibeSys execution policy to the joint runtime resource owner."""

    def resolve_configuration(role: AgentRole) -> AgentExecutionConfiguration:
        spec = agent_spec_from_config(
            request.config,
            backend=request.agent_backend,
            provider=request.cli_provider,
        )
        return AgentExecutionConfiguration(
            agent_id=role.id,
            spec=spec,
            reasoning_effort=spec.role_reasoning_efforts.get(role.id, spec.reasoning_effort),
        )

    return create_workspace_runtime(
        roles,
        root_resource=root_resource,
        supports_parallel_candidates=supports_parallel_candidates,
        create_candidate_resource=create_candidate_resource,
        resolve_configuration=resolve_configuration,
        session_store=lambda: session_store,
        control=control,
        lifecycle_events=lifecycle_events,
        agent_events=CoreAgentEventSink(events.record),
        route_message=splice_steering,
        blocking=blocking,
        client_factory=client_factory or build_agent_client,
        tool_bindings={
            tool_id: partial(resolver, tool_context)
            for tool_id, resolver in dict(tool_bindings or {}).items()
        },
        log=log,
    )


__all__ = ["_AgentToolResolver", "create_agents_and_workspaces"]
