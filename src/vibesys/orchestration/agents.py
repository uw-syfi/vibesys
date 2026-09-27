"""VibeSys composition for runtime-owned explicit agent sessions."""

# lint-waiver: session composition shares one private RunContext owner with sibling capabilities.
# ruff: noqa: SLF001

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.context import borrow_run_agent_environment, open_scoped_agent_environment
from vibesys.orchestration.steering import splice_steering
from vibesys.orchestration.workspaces import WorkspaceHandle
from vibesys.run.agent_events import CoreAgentEventSink
from vs_agent.api import build_agent_client
from vs_runtime.api import AgentRole, Workspace
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionEnvironment,
    AgentExecutionLifecycleSink,
    AgentExecutionScope,
    create_agent_session_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.context import _RunResources
    from vibesys.orchestration._host import HostResources
    from vibesys.orchestration.environment import AgentEnvironment
    from vs_agent.api import AgentClientProtocol, ToolServerDescriptor
    from vs_runtime.api import AgentSession
    from vs_runtime.api.infrastructure import RunControlChannel

type _AgentToolResolver = Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]


class _Agents:
    """Narrow explicit-session capability composed for one run."""

    def __init__(  # noqa: PLR0913  # lint-waiver: composition fixes independent runtime effects once; plugins see only create_session.
        self,
        host: HostResources,
        roles: tuple[AgentRole, ...],
        tool_bindings: Mapping[str, _AgentToolResolver] | None,
        *,
        control: RunControlChannel,
        lifecycle_events: AgentExecutionLifecycleSink,
        open_agent_environment: Callable[..., AgentEnvironment] | None,
        client_factory: Callable[..., AgentClientProtocol] | None,
    ) -> None:
        self._host = host
        self._open_agent_environment = open_agent_environment
        self._explicit = create_agent_session_runtime(
            roles,
            resolve_execution=self._resolve_execution,
            resolve_workspace=self._resolve_workspace,
            session_store=lambda: host._session_store,
            control=control,
            lifecycle_events=lifecycle_events,
            agent_events=CoreAgentEventSink(host.events.record),
            route_message=splice_steering,
            client_factory=client_factory or build_agent_client,
            tool_bindings={
                tool_id: partial(resolver, host)
                for tool_id, resolver in dict(tool_bindings or {}).items()
            },
            log=host.log,
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

    def _resolve_workspace(self, workspace: Workspace) -> WorkspaceHandle:
        if not isinstance(workspace, WorkspaceHandle):
            message = "workspace must be a live handle from this run"
            raise TypeError(message)
        self._host.workspaces._scope_of(workspace)
        return workspace

    def _resolve_execution(
        self,
        role: AgentRole,
        workspace: Workspace,
    ) -> tuple[AgentExecutionConfiguration, AgentExecutionScope]:
        managed = self._resolve_workspace(workspace)
        scope = self._host.workspaces._scope_of(managed)
        resources = self._host.workspaces._resources_for(scope)
        request = self._host.request
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
                root=scope is None,
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

    async def close_workspace(self, workspace: WorkspaceHandle) -> None:
        """Close workspace-bound sessions before environment teardown."""
        await self._explicit.close_workspace(workspace)

    async def close(self) -> None:
        """Close explicit sessions in reverse creation order."""
        await self._explicit.close()

    def _mark_closed(self) -> None:
        """Reject new sessions and turns before run-owned teardown starts."""
        self._explicit.begin_close()


__all__ = ["_AgentToolResolver", "_Agents"]
