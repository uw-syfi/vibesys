"""Production ownership of explicit orchestration-agent sessions.

The plugin-facing contracts live in :mod:`vs_runtime.contracts`.  This module
owns their production lifecycle while product composition supplies the
temporary execution and workspace adapters needed during runtime extraction.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, TypeVar, overload

from pydantic import BaseModel

from vs_agent.api import AgentOutputSchemaError, AgentSessionKey, AgentSpawnError, SessionScope
from vs_agent.api import AgentTurnTimeoutError as DriverAgentTurnTimeoutError
from vs_runtime._agent_declarations import (
    validate_agent_capabilities,
    validate_extra_tools,
)
from vs_runtime._agent_execution import RuntimeAgentExecution
from vs_runtime._workspace_access import unauthorized_paths
from vs_runtime.contracts import (
    AgentBinding,
    AgentCapability,
    AgentRole,
    AgentToolBindingContext,
    AgentTurnTimeoutError,
    RuntimeContractError,
    SessionClosedError,
    StructuredResponseError,
    UnknownAgentRoleError,
    Workspace,
    WorkspaceAccess,
    validate_member_id,
    validate_workspace_writable_paths,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_agent.api import (
        AgentCapabilities,
        AgentClientProtocol,
        AgentEventSink,
        SessionStore,
        ToolServerDescriptor,
    )
    from vs_runtime._agent_execution import (
        AgentExecutionLifecycleSink,
        AgentMessageRouter,
    )
    from vs_runtime._run_control import RunControlChannel
    from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces
    from vs_runtime.api.infrastructure import (
        AgentConfigurationResolver,
        AgentToolResolver,
    )

ResponseT = TypeVar("ResponseT", bound=BaseModel)


def _supports_required_capability(
    capabilities: AgentCapabilities,
    capability: AgentCapability,
) -> bool:
    """Translate plugin capability names to the agent client's contract."""
    if capability is AgentCapability.MCP_SERVERS:
        return capabilities.tool_servers
    return bool(getattr(capabilities, capability.value))


class RuntimeAgentSession:
    """One context-preserving conversation owned by a runtime."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-837201 [PLR0913]; immutable session configuration and its owned execution resource are independent inputs.
        self,
        execution: RuntimeAgentExecution,
        role: AgentRole,
        workspace: RuntimeWorkspace,
        member_id: str | None,
        writable_paths: tuple[str, ...],
        writable_directory_paths: tuple[str, ...],
        tool_servers: tuple[ToolServerDescriptor, ...],
        *,
        session_id: str,
        log: Callable[[str], None],
    ) -> None:
        self._execution = execution
        self._role = role
        self._workspace = workspace
        self._member_id = member_id
        self._writable_paths = writable_paths
        self._writable_directory_paths = writable_directory_paths
        self._tool_servers = tool_servers
        self._log = log
        self._binding = AgentBinding(
            backend=execution.backend_name,
            driver=execution.driver_name,
            provider=execution.provider,
            model=execution.model,
            reasoning_effort=execution.reasoning_effort,
        )
        self._session_key = (
            AgentSessionKey(SessionScope.MEMBER, f"{role.id}:{member_id}")
            if member_id is not None
            else AgentSessionKey(SessionScope.ROLE, f"session:{session_id}")
        )
        self._turn_number = 0
        self._turn_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def role(self) -> AgentRole:
        return self._role

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def member_id(self) -> str | None:
        return self._member_id

    @property
    def writable_paths(self) -> tuple[str, ...]:
        return self._writable_paths

    @property
    def binding(self) -> AgentBinding:
        return self._binding

    @property
    def closed(self) -> bool:
        return self._closed

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self,
        message: str,
        *,
        response: type[ResponseT] | None = None,
    ) -> str | ResponseT:
        if self._closed:
            raise SessionClosedError
        async with self._turn_lock:
            if self._closed:
                raise SessionClosedError
            return await self._turn_once(message, response=response)

    async def _turn_once(
        self,
        message: str,
        *,
        response: type[ResponseT] | None,
    ) -> str | ResponseT:
        self._turn_number += 1
        label = f"{self._role.id}-session-turn-{self._turn_number}"
        revision = await self._workspace.snapshot(f"{label}-input")
        try:
            try:
                result = await self._execution.execute(
                    message,
                    system_prompt=self._role.system_prompt,
                    response=response,
                    label=label,
                    session_key=self._session_key,
                    tool_servers=self._tool_servers or None,
                )
            except DriverAgentTurnTimeoutError as error:
                raise AgentTurnTimeoutError(error.timeout_seconds) from error
            except (OSError, ImportError) as error:
                raise AgentSpawnError(
                    self._binding.provider or self._binding.backend, str(error)
                ) from error
            except AgentOutputSchemaError as error:
                if response is None:
                    raise
                raise StructuredResponseError(
                    self._role.id, response, detail=error.detail
                ) from error
        finally:
            await self._enforce_workspace_access(revision)

        if (
            self._role.workspace_access is WorkspaceAccess.READ_WRITE
            or await self._workspace.pending_changes()
        ):
            await self._workspace.snapshot(label)
        return result

    async def _enforce_workspace_access(self, revision: str) -> None:
        if self._role.workspace_access not in {
            WorkspaceAccess.READ_ONLY,
            WorkspaceAccess.LIMITED,
        }:
            return
        allowed = (
            self._writable_paths if self._role.workspace_access is WorkspaceAccess.LIMITED else ()
        )
        directories = (
            self._writable_directory_paths
            if self._role.workspace_access is WorkspaceAccess.LIMITED
            else ()
        )
        unauthorized = unauthorized_paths(
            await self._workspace.pending_changes(),
            allowed,
            directories=directories,
        )
        if not unauthorized:
            return
        await self._workspace.restore_for_agent(
            revision,
            preserve_paths=allowed,
        )
        remaining = unauthorized_paths(
            await self._workspace.pending_changes(),
            allowed,
            directories=directories,
        )
        if remaining:
            message = (
                f"role {self._role.id!r} left unauthorized workspace changes: "
                f"{', '.join(remaining)}"
            )
            raise RuntimeContractError(message)
        self._log(
            f"[role-isolation] reverted {len(unauthorized)} workspace change(s) "
            f"attempted by {self._role.id}: {', '.join(unauthorized[:8])}"
        )

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        async with self._turn_lock:
            await self._execution.close()

    def mark_closed(self) -> None:
        self._closed = True

    def cancel(self) -> None:
        """Reject further turns and stop the active provider turn, if any."""
        self._closed = True
        self._execution.cancel()


class RuntimeAgentSessions:
    """Production factory and reverse-order owner for explicit sessions."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-837211 [PLR0913]; run composition injects independent lower-layer effects once; session callers see only create_session.
        self,
        roles: tuple[AgentRole, ...],
        *,
        workspaces: RuntimeWorkspaces,
        resolve_configuration: AgentConfigurationResolver,
        session_store: Callable[[], SessionStore | None],
        control: RunControlChannel,
        lifecycle_events: AgentExecutionLifecycleSink,
        agent_events: AgentEventSink,
        route_message: AgentMessageRouter,
        client_factory: Callable[..., AgentClientProtocol],
        tool_bindings: Mapping[str, AgentToolResolver] | None,
        log: Callable[[str], None],
    ) -> None:
        self._roles = {role.id: role for role in roles}
        self._workspaces = workspaces
        self._resolve_configuration = resolve_configuration
        self._session_store = session_store
        self._control = control
        self._lifecycle_events = lifecycle_events
        self._agent_events = agent_events
        self._route_message = route_message
        self._client_factory = client_factory
        self._tool_bindings = dict(tool_bindings or {})
        self._log = log
        self._sessions: list[RuntimeAgentSession] = []
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> RuntimeAgentSession:
        async with self._lifecycle_lock:
            if self._closed:
                raise SessionClosedError
            validate_member_id(member_id)
            if self._roles.get(role.id) != role:
                raise UnknownAgentRoleError(role.id)
            validated_paths = validate_workspace_writable_paths(
                role.workspace_access,
                writable_paths,
            )
            bound_tool_ids = validate_extra_tools(role, self._tool_bindings.keys())

            managed_workspace = self._workspaces.workspace_for(workspace)
            async with self._workspaces._mutation(managed_workspace):  # noqa: SLF001  # lint-waiver: LW-837220 [SLF001]; session construction holds the owning workspace alive through execution binding.
                session_id = uuid.uuid4().hex
                configuration = self._resolve_configuration(role)
                scope = self._workspaces.resource_for(managed_workspace).agent_scope()
                execution = await RuntimeAgentExecution.open(
                    configuration,
                    scope,
                    session_store=self._session_store(),
                    control=self._control,
                    lifecycle=self._lifecycle_events,
                    agent_events=self._agent_events,
                    route_message=self._route_message,
                    client_factory=self._client_factory,
                )
                try:
                    binding_context = AgentToolBindingContext(
                        role=role,
                        workspace=managed_workspace,
                        member_id=member_id,
                        agent_path=execution.agent_path,
                    )
                    tool_servers = tuple(
                        spec
                        for tool_id in bound_tool_ids
                        for spec in self._tool_bindings[tool_id](binding_context)
                    )
                    session = self._complete_session(
                        execution,
                        role,
                        managed_workspace,
                        member_id,
                        validated_paths,
                        bound_tool_ids,
                        tool_servers,
                        session_id=session_id,
                    )
                except BaseException as error:
                    try:
                        await execution.close()
                    except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-837202 [BLE001]; preserve the construction failure while reporting cleanup failure.
                        error.add_note(f"agent execution cleanup also failed: {cleanup_error}")
                    raise
                self._sessions.append(session)
                return session

    def _complete_session(  # noqa: PLR0913  # lint-waiver: LW-837203 [PLR0913]; the helper validates the independently fixed session declaration before transferring ownership.
        self,
        execution: RuntimeAgentExecution,
        role: AgentRole,
        workspace: RuntimeWorkspace,
        member_id: str | None,
        writable_paths: tuple[str, ...],
        bound_tool_ids: tuple[str, ...],
        tool_servers: tuple[ToolServerDescriptor, ...],
        session_id: str,
    ) -> RuntimeAgentSession:
        if self._closed:
            raise SessionClosedError
        supported_capabilities = frozenset(
            capability
            for capability in AgentCapability
            if _supports_required_capability(execution.capabilities, capability)
        )
        validate_agent_capabilities(
            role,
            supported_capabilities,
            member_id=member_id,
            has_bound_tools=bool(bound_tool_ids),
        )
        return RuntimeAgentSession(
            execution,
            role,
            workspace,
            member_id,
            writable_paths,
            tuple(path for path in writable_paths if workspace.is_directory(path)),
            tool_servers,
            session_id=session_id,
            log=self._log,
        )

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        errors: list[BaseException] = []
        async with self._lifecycle_lock:
            for session in reversed(self._sessions):
                try:
                    await session.close()
                except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-837204 [BLE001]; every owned session must be closed before grouped cleanup errors propagate.
                    errors.append(error)
        if errors:
            message = "agent session cleanup failed"
            raise BaseExceptionGroup(message, errors)

    async def close_workspace(self, workspace: Workspace) -> None:
        """Close matching sessions in reverse order before workspace teardown."""
        errors: list[BaseException] = []
        async with self._lifecycle_lock:
            managed_workspace = self._workspaces.workspace_for(workspace)
            sessions = [
                session for session in self._sessions if session.workspace is managed_workspace
            ]
            for session in sessions:
                session.mark_closed()
            for session in reversed(sessions):
                try:
                    await session.close()
                except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-837212 [BLE001]; every workspace-bound session must close before grouped failures propagate.
                    errors.append(error)
            selected = set(sessions)
            self._sessions = [session for session in self._sessions if session not in selected]
        if errors:
            message = "workspace agent session cleanup failed"
            raise BaseExceptionGroup(message, errors)

    def begin_close(self) -> None:
        self._closed = True
        for session in self._sessions:
            session.cancel()
