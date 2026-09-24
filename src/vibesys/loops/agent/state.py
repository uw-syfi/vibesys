"""The single durable state slot for agent orchestrators."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.model import AgentRunState

if TYPE_CHECKING:
    from vs_project.api import StateNamespace, StateTransition


class AgentRunStateStore:
    """Persist agent policy state in the run's portable v4 namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the single typed state slot in the supplied namespace."""
        self._namespace = namespace
        self._slot = namespace.slot("state.json", AgentRunState)

    def load_optional(self) -> AgentRunState | None:
        """Return the authoritative aggregate when present."""
        return self._slot.load_optional()

    def load(self) -> AgentRunState:
        """Return the aggregate or a new empty state."""
        return self.load_optional() or AgentRunState()

    def save(self, state: AgentRunState) -> None:
        """Atomically replace the policy's portable state."""
        self._slot.save(state)

    def transition(self, state: AgentRunState) -> StateTransition:
        """Prepare an exact replacement for the round transaction."""
        return self._slot.transition(state)

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace used for Git snapshots."""
        return self._namespace
