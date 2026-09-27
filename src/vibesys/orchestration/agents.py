"""VibeSys composition for runtime-owned explicit agent sessions."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vibesys.composition import agent_spec_from_config
from vibesys.context import borrow_run_agent_environment, open_scoped_agent_environment
from vibesys.orchestration.steering import splice_steering
from vibesys.orchestration.workspace_resources import resources_for
from vibesys.run.agent_events import CoreAgentEventSink
from vs_agent.api import build_agent_client
from vs_runtime.api import AgentRole, Workspace
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionEnvironment,
    AgentExecutionLifecycleSink,
    AgentExecutionScope,
    AgentWorkspaceRuntime,
    CandidateWorkspaceResourceFactory,
    WorkspaceResource,
    create_agent_workspace_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.context import _RunResources
    from vibesys.events import CoreEventWriter
    from vibesys.orchestration.request import RunRequest
    from vs_agent.api import AgentClientProtocol, DurableSessionStore, ToolServerDescriptor
    from vs_runtime.api.infrastructure import RunControlChannel

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
    agent_environment_opener: Callable[..., AgentExecutionEnvironment] | None,
    client_factory: Callable[..., AgentClientProtocol] | None,
) -> AgentWorkspaceRuntime:
    """Bind VibeSys execution policy to the joint runtime resource owner."""
    runtime: AgentWorkspaceRuntime

    def resolve_execution(
        role: AgentRole,
        workspace: Workspace,
    ) -> tuple[AgentExecutionConfiguration, AgentExecutionScope]:
        resources = resources_for(runtime.workspaces, workspace)
        spec = agent_spec_from_config(
            request.config,
            backend=request.agent_backend,
            provider=request.cli_provider,
        )
        configuration = AgentExecutionConfiguration(
            agent_id=role.id,
            spec=spec,
            reasoning_effort=spec.role_reasoning_efforts.get(role.id, spec.reasoning_effort),
        )
        execution_scope = AgentExecutionScope(
            workspace_path=resources.workspace,
            log_directory=resources.log_dir,
            open_environment=partial(
                open_environment,
                resources,
                root=workspace is runtime.workspaces.root,
            ),
            current_log_file=lambda: resources.run_log_file,
            environment_variables=resources.device.gpu_env,
        )
        return configuration, execution_scope

    def open_environment(
        resources: _RunResources,
        configuration: AgentExecutionConfiguration,
        *,
        root: bool,
    ) -> AgentExecutionEnvironment:
        if resources.run_environment_view.share_agent_session:
            return borrow_run_agent_environment(
                resources,
                mounts=configuration.resources,
                agent_backend=configuration.spec.backend.value,
                cli_provider=configuration.spec.provider,
            )
        if root and agent_environment_opener is not None:
            return agent_environment_opener(
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

    runtime = create_agent_workspace_runtime(
        roles,
        root_resource=root_resource,
        supports_parallel_candidates=supports_parallel_candidates,
        create_candidate_resource=create_candidate_resource,
        resolve_execution=resolve_execution,
        session_store=lambda: session_store,
        control=control,
        lifecycle_events=lifecycle_events,
        agent_events=CoreAgentEventSink(events.record),
        route_message=splice_steering,
        client_factory=client_factory or build_agent_client,
        tool_bindings={
            tool_id: partial(resolver, tool_context)
            for tool_id, resolver in dict(tool_bindings or {}).items()
        },
        log=log,
    )
    return runtime


__all__ = ["_AgentToolResolver", "create_agents_and_workspaces"]
