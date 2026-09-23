"""Public capabilities available to a custom orchestration while it executes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import AgentSpec
    from vs_sandbox.api import HostResource


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
