"""Read canonical agent policy state from a v4 run manifest."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.orchestration import AGENT_ORCHESTRATION_IDS, options_from_descriptor
from vibesys.loops.agent.state import AgentRunStateStore

if TYPE_CHECKING:
    from vibesys.loops.agent.model import AgentRunState
    from vs_project.api import OrchestrationRunManifest, Project


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
