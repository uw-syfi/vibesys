"""Public capabilities available to a custom orchestration while it executes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentSessionKey, AgentSpec, MCPServerSpec
    from vs_sandbox.api import HostResource

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """One named agent with its own backend/model choice and host grants."""

    id: str
    spec: AgentSpec
    resources: tuple[HostResource, ...] = ()


class AgentHandle(Protocol):
    """A live agent conversation owned by one runtime."""

    def turn(
        self,
        message: str,
        *,
        system_prompt: str = "",
        label: str = "",
    ) -> str:
        """Send one turn and return its text; labels are policy-defined."""
        ...

    def turn_structured(  # noqa: PLR0913
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

    def close(self) -> None:
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

    def spawn_agent(self, definition: AgentDefinition) -> AgentHandle:
        """Open an independently configured agent in this run's environment."""
        ...
