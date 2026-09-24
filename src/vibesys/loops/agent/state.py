"""The single durable state slot for agent orchestrators."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.model import AgentRunState
from vibesys.loops.agent.orchestration import AGENT_ORCHESTRATION_IDS, options_from_descriptor

if TYPE_CHECKING:
    from vs_project.api import OrchestrationRunManifest, Project, StateNamespace, StateTransition


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


def agent_run_objectives(manifest: OrchestrationRunManifest) -> tuple[str, ...] | None:
    """Derive directed axes from the agent policy's persisted metric space."""
    if manifest.orchestration.id not in AGENT_ORCHESTRATION_IDS:
        return None
    space = options_from_descriptor(manifest.orchestration).metric_space
    return tuple(f"{axis.name}:{axis.direction}" for axis in space.objectives)


def is_agent_run_manifest(manifest: OrchestrationRunManifest) -> bool:
    """Identify all four agent strategies from their descriptor IDs."""
    return manifest.orchestration.id in AGENT_ORCHESTRATION_IDS


def load_agent_run_state(project: Project, run_id: str) -> AgentRunState | None:
    """Load one agent run's state without legacy format recovery."""
    manifest = project.state.load_run(run_id)
    if not is_agent_run_manifest(manifest):
        return None
    return AgentRunStateStore(project.state.portable_namespace(run_id, "agent")).load_optional()
