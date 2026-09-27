"""VibeSys composition for runtime-owned explicit agent sessions."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

from vibesys.agent_spec_config import agent_spec_from_config
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
    ManagedAgentWorkspace,
    create_agent_session_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.context import _RunResources
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.event_journal import EventJournal
    from vs_agent.api import AgentClientProtocol, DurableSessionStore, ToolServerDescriptor
    from vs_runtime.api import AgentSession, Workspaces
    from vs_runtime.api.infrastructure import RunControlChannel

type _AgentToolResolver = Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]


class _Agents:
    """Narrow explicit-session capability composed for one run."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-837215 [PLR0913]; composition fixes independent runtime effects once; plugins see only create_session.
        self,
        request: RunRequest,
        workspaces: Workspaces,
        session_store: DurableSessionStore,
        events: EventJournal,
        log: Callable[[str], None],
        tool_context: object,
        roles: tuple[AgentRole, ...],
        tool_bindings: Mapping[str, _AgentToolResolver] | None,
        *,
        control: RunControlChannel,
        lifecycle_events: AgentExecutionLifecycleSink,
        open_agent_environment: Callable[..., AgentEnvironment] | None,
        client_factory: Callable[..., AgentClientProtocol] | None,
    ) -> None:
        self._request = request
        self._workspaces = workspaces
        self._open_agent_environment = open_agent_environment
        self._explicit = create_agent_session_runtime(
            roles,
            resolve_execution=self._resolve_execution,
            resolve_workspace=self._resolve_workspace,
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

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        """Create a role session, durable only when policy supplies a member ID."""
        return await self._explicit.create_session(
            role,
            workspace=workspace,
            member_id=member_id,
            writable_paths=writable_paths,
        )

    def _resolve_workspace(self, workspace: Workspace) -> ManagedAgentWorkspace:
        resources_for(self._workspaces, workspace)
        return cast("ManagedAgentWorkspace", workspace)

    def _resolve_execution(
        self,
        role: AgentRole,
        workspace: Workspace,
    ) -> tuple[AgentExecutionConfiguration, AgentExecutionScope]:
        managed = self._resolve_workspace(workspace)
        resources = resources_for(self._workspaces, managed)
        request = self._request
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
                self._open_environment,
                resources,
                root=managed is self._workspaces.root,
            ),
            current_log_file=lambda: resources.run_log_file,
            environment_variables=resources.device.gpu_env,
        )
        return configuration, execution_scope

    def _open_environment(
        self,
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
        if root and self._open_agent_environment is not None:
            return self._open_agent_environment(
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

    async def close_workspace(self, workspace: Workspace) -> None:
        """Close workspace-bound sessions before environment teardown."""
        await self._explicit.close_workspace(workspace)

    async def close(self) -> None:
        """Close explicit sessions in reverse creation order."""
        await self._explicit.close()

    def begin_close(self) -> None:
        """Reject new sessions and turns before run-owned teardown starts."""
        self._explicit.begin_close()


__all__ = ["_AgentToolResolver", "_Agents"]
