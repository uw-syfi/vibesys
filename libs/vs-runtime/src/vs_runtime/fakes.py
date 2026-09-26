"""Faithful in-memory implementations of the public runtime contracts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias, TypeVar, overload

from pydantic import BaseModel

from vs_runtime.contracts import (
    AgentRole,
    AgentSession,
    OrchestrationPlugin,
    SessionClosedError,
    UnknownAgentRoleError,
    Workspace,
    validate_member_id,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)
TurnResponder: TypeAlias = Callable[
    [AgentRole, tuple[str, ...], str, type[BaseModel] | None], object
]


def _echo_responder(
    _role: AgentRole,
    _history: tuple[str, ...],
    message: str,
    _response: type[BaseModel] | None,
) -> object:
    return message


class FakeAgentSession:
    """In-memory conversation with the public lifetime and context contract."""

    def __init__(
        self,
        role: AgentRole,
        workspace: Workspace,
        member_id: str | None,
        responder: TurnResponder,
    ) -> None:
        """Bind one fresh session to immutable creation configuration."""
        self._role = role
        self._workspace = workspace
        self._member_id = member_id
        self._responder = responder
        self._history: list[str] = []
        self._closed = False

    @property
    def role(self) -> AgentRole:
        """Return the role bound at creation."""
        return self._role

    @property
    def workspace(self) -> Workspace:
        """Return the workspace bound at creation."""
        return self._workspace

    @property
    def member_id(self) -> str | None:
        """Return optional policy attribution for this instance."""
        return self._member_id

    @property
    def closed(self) -> bool:
        """Return whether cleanup has ended this session."""
        return self._closed

    @property
    def history(self) -> tuple[str, ...]:
        """Return completed user messages in conversation order."""
        return tuple(self._history)

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self, message: str, *, response: type[ResponseT] | None = None
    ) -> str | ResponseT:
        """Respond from prior completed turns, then append this message."""
        if self._closed:
            raise SessionClosedError
        value = self._responder(self._role, tuple(self._history), message, response)
        if response is None:
            if not isinstance(value, str):
                message = "text turn responder must return str"
                raise TypeError(message)
            result: str | ResponseT = value
        else:
            result = response.model_validate(value)
        self._history.append(message)
        return result

    async def close(self) -> None:
        """End the fake conversation idempotently."""
        self._closed = True


class FakeAgentSessions:
    """Run-owned in-memory factory with isolated creation semantics."""

    def __init__(
        self,
        agents: tuple[AgentRole, ...],
        *,
        responder: TurnResponder = _echo_responder,
    ) -> None:
        """Build the private role lookup from the plugin's authoritative tuple."""
        self._roles = {role.id: role for role in agents}
        self._responder = responder
        self._sessions: list[FakeAgentSession] = []
        self._closed = False

    @property
    def sessions(self) -> tuple[FakeAgentSession, ...]:
        """Return created sessions in ownership order."""
        return tuple(self._sessions)

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
    ) -> AgentSession:
        """Validate the declared role and create an independent conversation."""
        if self._closed:
            raise SessionClosedError
        if self._roles.get(role.id) != role:
            raise UnknownAgentRoleError(role.id)
        validate_member_id(member_id)
        session = FakeAgentSession(role, workspace, member_id, self._responder)
        self._sessions.append(session)
        return session

    async def close(self) -> None:
        """Close all sessions in reverse creation order, once."""
        if self._closed:
            return
        self._closed = True
        for session in reversed(self._sessions):
            await session.close()


class FakeRunHost:
    """In-memory run host that owns fake sessions and captured log lines."""

    def __init__(
        self,
        plugin: OrchestrationPlugin,
        *,
        run_id: str = "test-run",
        project_root: Path = Path(),
        responder: TurnResponder = _echo_responder,
    ) -> None:
        """Create a host whose private role map derives from ``plugin.agents``."""
        self._run_id = run_id
        self._project_root = project_root
        self._agents = FakeAgentSessions(plugin.agents, responder=responder)
        self._logs: list[str] = []
        self._closed = False

    @property
    def run_id(self) -> str:
        """Return this fake run's stable identity."""
        return self._run_id

    @property
    def project_root(self) -> Path:
        """Return the configured fake project handle."""
        return self._project_root

    @property
    def agents(self) -> FakeAgentSessions:
        """Return the run-owned fake session factory."""
        return self._agents

    @property
    def logs(self) -> tuple[str, ...]:
        """Return recorded log messages in call order."""
        return tuple(self._logs)

    def log(self, message: str) -> None:
        """Capture one log message."""
        self._logs.append(message)

    async def close(self) -> None:
        """Close all run-owned resources idempotently."""
        if self._closed:
            return
        self._closed = True
        await self._agents.close()
