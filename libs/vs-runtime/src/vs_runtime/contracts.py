"""Policy-facing contracts for the reusable orchestration runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path  # Pydantic resolves WorkspaceRef at runtime.
from typing import TYPE_CHECKING, Protocol, TypeVar, overload

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class RuntimeContractError(RuntimeError):
    """Base class for rejected runtime operations."""


class SessionClosedError(RuntimeContractError):
    """A caller used an agent session after its lifetime ended."""

    def __init__(self) -> None:
        """Describe a turn attempted after session cleanup."""
        super().__init__("agent session is closed")


class StructuredResponseError(RuntimeContractError):
    """An agent turn could not be parsed as its requested response type."""

    def __init__(self, role_id: str, response_type: type[BaseModel]) -> None:
        """Name the role and response contract whose validation failed."""
        super().__init__(
            f"agent role {role_id!r} did not return a valid {response_type.__name__} response"
        )


class UnknownAgentRoleError(RuntimeContractError):
    """A session was requested for a role outside the plugin's agent tuple."""

    def __init__(self, role_id: str) -> None:
        """Name the role not owned by the active plugin."""
        super().__init__(f"agent role {role_id!r} is not declared by this orchestration")


class WorkspaceAccess(StrEnum):
    """Workspace mutation authority enforced for one role."""

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class AgentCapability(StrEnum):
    """Closed driver capabilities a role may require."""

    MCP_SERVERS = "mcp_servers"
    NESTED_READ_ONLY_PATHS = "nested_read_only_paths"
    HIDDEN_PATHS = "hidden_paths"
    HOST_PATH_GRANTS = "host_path_grants"
    CONTAINER_EXECUTION = "container_execution"
    TIMEOUTS = "timeouts"
    SESSION_REUSE = "session_reuse"
    PROVIDER_SESSION_RESUME = "provider_session_resume"


class AgentTool(BaseModel):
    """Minimal immutable reference to one registered agent tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")


class Workspace(Protocol):
    """Stable identity and host path of one live run workspace."""

    @property
    def id(self) -> str | None:
        """Return the isolated workspace ID, or ``None`` for the run root."""
        ...

    @property
    def path(self) -> Path:
        """Return the workspace's host path."""
        ...


class WorkspaceRef(BaseModel):
    """Immutable workspace value suitable for adapters and tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str | None = Field(default=None, min_length=1)
    path: Path


class AgentRole(BaseModel):
    """Complete immutable declaration of one policy-owned agent role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    system_prompt: str
    tools: tuple[AgentTool, ...] = ()
    skills: tuple[str, ...] = ()
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_WRITE
    required_capabilities: frozenset[AgentCapability] = frozenset()


class AgentSession(Protocol):
    """One configured conversation with sequential, context-preserving turns."""

    @property
    def role(self) -> AgentRole:
        """Return the immutable role bound when this session was created."""
        ...

    @property
    def workspace(self) -> Workspace:
        """Return the immutable workspace handle bound at creation."""
        ...

    @property
    def member_id(self) -> str | None:
        """Return optional policy attribution for this session instance."""
        ...

    @property
    def closed(self) -> bool:
        """Return whether this session can accept more turns."""
        ...

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self, message: str, *, response: type[ResponseT] | None = None
    ) -> str | ResponseT:
        """Add one turn, returning text or a value validated as ``response``."""
        ...

    async def close(self) -> None:
        """Release session resources; safe to call more than once."""
        ...


class AgentSessions(Protocol):
    """Run-owned factory and lifetime owner for agent conversations."""

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
    ) -> AgentSession:
        """Create a fresh conversation bound to immutable session configuration."""
        ...

    async def close(self) -> None:
        """Close every owned session in reverse creation order."""
        ...


class Workspaces(Protocol):
    """Run-owned access to live workspaces."""

    @property
    def root(self) -> Workspace:
        """Return the live root workspace for this run."""
        ...


class RunStatus(StrEnum):
    """Terminal status returned by orchestration policy."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


class RunHost(Protocol):
    """Small run-owned capability surface initially required by orchestration."""

    @property
    def run_id(self) -> str:
        """Return this run's stable identity."""
        ...

    @property
    def agents(self) -> AgentSessions:
        """Return the run-owned agent-session capability."""
        ...

    @property
    def workspaces(self) -> Workspaces:
        """Return this run's live workspace capability."""
        ...

    def log(self, message: str) -> None:
        """Record a presentation-neutral run log message."""
        ...


@dataclass(frozen=True, slots=True)
class OrchestrationPlugin:
    """One validated orchestration and every policy value it owns.

    ``agents`` is the sole role catalog. Runtime implementations may construct
    a private lookup from it, but no second public registry can disagree.
    """

    id: str
    agents: tuple[AgentRole, ...]
    options: type[BaseModel]
    orchestrate: Callable[[RunHost, BaseModel], Awaitable[RunStatus]]
    state: type[BaseModel] | None = None
    project: Callable[[BaseModel], BaseModel] | None = None

    def __post_init__(self) -> None:
        """Reject duplicate role IDs before any run resources open."""
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", self.id) is None:
            message = f"invalid orchestration plugin ID {self.id!r}"
            raise ValueError(message)
        seen: set[str] = set()
        duplicates: set[str] = set()
        for role in self.agents:
            if role.id in seen:
                duplicates.add(role.id)
            seen.add(role.id)
        if duplicates:
            message = f"duplicate agent role IDs: {', '.join(sorted(duplicates))}"
            raise ValueError(message)


def validate_member_id(member_id: str | None) -> None:
    """Reject an invalid optional stable member identifier."""
    if member_id is not None and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", member_id) is None:
        message = f"invalid agent member ID {member_id!r}"
        raise ValueError(message)
