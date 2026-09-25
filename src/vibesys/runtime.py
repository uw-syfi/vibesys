"""Public capabilities available to a custom orchestration while it executes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentCapabilities, AgentSessionKey, AgentSpec, MCPServerSpec
    from vs_sandbox.api import HostResource

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """One named agent with its own backend/model choice and host grants."""

    id: str
    spec: AgentSpec
    resources: tuple[HostResource, ...] = ()


@dataclass(slots=True)
class WorkspaceScope:
    """An isolated candidate tree and its current retained revision."""

    id: str
    path: Path
    revision: str


class AgentHandle(Protocol):
    """A live agent conversation owned by one runtime."""

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return features the selected driver can enforce."""
        ...

    @property
    def backend_name(self) -> str:
        """Return the selected agent backend."""
        ...

    @property
    def driver_name(self) -> str | None:
        """Return the selected CLI driver, when configured."""
        ...

    @property
    def provider(self) -> str | None:
        """Return the selected provider, when configured."""
        ...

    @property
    def model(self) -> str | None:
        """Return the model used for this role."""
        ...

    async def turn(
        self,
        message: str,
        *,
        system_prompt: str = "",
        label: str = "",
    ) -> str:
        """Send one turn and return its text; labels are policy-defined."""
        ...

    async def turn_structured(  # noqa: PLR0913  # lint-waiver: LW-020037 [PLR0913]; AgentHandle.turn_structured is the public typed-turn contract, and its keyword options are independent.
        self,
        message: str,
        *,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        system_prompt: str = "",
        label: str = "",
        session_key: AgentSessionKey | None = None,
        reuse_session: bool | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
    ) -> T:
        """Run a typed turn, retaining the caller's fallback and session policy."""
        ...

    async def close(self) -> None:
        """Release this agent and its sandbox; safe to call more than once."""
        ...


class VibeSysRuntime(Protocol):
    """Run-scoped host for agents; orchestration owns scheduling and messages.

    Spawned agents and their sandboxes close when execution ends. A policy may
    close a handle earlier. `resources` are passed to sandbox provisioning and
    agent-driver enforcement, so a driver unable to enforce them rejects spawn.
    """

    @property
    def workspace(self) -> Path:
        """Return the prepared run's writable project workspace."""
        ...

    async def spawn(
        self, definition: AgentDefinition, *, scope: WorkspaceScope | None = None
    ) -> AgentHandle:
        """Open an agent in the parent or a live isolated workspace."""
        ...
