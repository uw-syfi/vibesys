"""Agent orchestration's public projection and compatibility helpers.

Generic run/session contracts live in :mod:`vibesys.api`. Import this module
when an application explicitly handles the built-in agent policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.agent_run.issue_board import framework_memory_paths
from vibesys.agent_run.options import options_from_descriptor
from vibesys.agent_run.readmodel import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
    agent_projection,
)

if TYPE_CHECKING:
    from vs_project.api import OrchestrationRunManifest


def is_agent_run_manifest(manifest: OrchestrationRunManifest) -> bool:
    """Identify runs whose registered projection uses agent-run state."""
    from vibesys.loops.registry import built_in_orchestrations  # noqa: PLC0415

    try:
        registration = built_in_orchestrations().resolve(manifest.orchestration.id)
    except ValueError:
        return False
    return registration.state_family == "agent"


def agent_run_objectives(manifest: OrchestrationRunManifest) -> tuple[str, ...] | None:
    """Return the directed axes for a registered agent-run descriptor."""
    if not is_agent_run_manifest(manifest):
        return None
    space = options_from_descriptor(manifest.orchestration).metric_space
    return tuple(f"{axis.name}:{axis.direction}" for axis in space.objectives)


__all__ = [
    "AgentRunProjection",
    "HypothesisRoundView",
    "HypothesisView",
    "RoundView",
    "agent_projection",
    "agent_run_objectives",
    "framework_memory_paths",
    "is_agent_run_manifest",
]
