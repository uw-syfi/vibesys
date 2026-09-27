"""Agent orchestration's public projection and compatibility helpers.

Generic run/session contracts live in :mod:`vibesys.api`. Import this module
when an application explicitly handles the built-in agent policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.hypothesis.readmodel import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
    agent_projection,
)
from vs_loop_state.api import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisResolution,
    JudgeVerdict,
    PerfDeltaReason,
)

if TYPE_CHECKING:
    from vs_project.api import OrchestrationRunManifest


def is_agent_run_manifest(manifest: OrchestrationRunManifest) -> bool:
    """Identify runs whose registered projection uses agent-run state."""
    # lint-waiver: LW-020002 [PLC0415]; the product catalog imports every built-in policy, so it loads only when a caller needs it.
    from vibesys.plugin_catalog import built_in_orchestrations  # noqa: PLC0415

    try:
        registration = built_in_orchestrations().resolve(manifest.orchestration.id)
    except ValueError:
        return False
    return registration.state_family == "agent"


def agent_run_objectives(manifest: OrchestrationRunManifest) -> tuple[str, ...] | None:
    """Return the directed axes for a registered agent-run descriptor."""
    # lint-waiver: LW-020003 [PLC0415]; the product catalog imports every built-in policy, so it loads only when a caller needs it.
    from vibesys.plugin_catalog import built_in_orchestrations  # noqa: PLC0415

    try:
        registration = built_in_orchestrations().resolve(manifest.orchestration.id)
    except ValueError:
        return None
    if registration.state_family != "agent":
        return None
    options = registration.parse_options(manifest.orchestration)
    if not isinstance(options, AgentOrchestrationOptions):
        message = f"agent orchestration {manifest.orchestration.id!r} has incompatible options"
        raise TypeError(message)
    space = options.metric_space
    return tuple(f"{axis.name}:{axis.direction}" for axis in space.objectives)


__all__ = [
    "AgentRunProjection",
    "CandidateDisposition",
    "HypothesisOutcome",
    "HypothesisResolution",
    "HypothesisRoundView",
    "HypothesisView",
    "JudgeVerdict",
    "PerfDeltaReason",
    "RoundView",
    "agent_projection",
    "agent_run_objectives",
    "is_agent_run_manifest",
]
