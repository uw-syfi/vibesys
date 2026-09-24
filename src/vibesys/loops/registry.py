"""Composition point for concrete built-in orchestrators."""

from __future__ import annotations

from vibesys.loops.agent.entrypoint import (
    AgentProjector,
    MultiAgentOrchestrator,
    ProfileGuidedMultiAgentOrchestrator,
    ProfileGuidedSingleAgentOrchestrator,
    SingleAgentOrchestrator,
)
from vibesys.loops.evolve.entrypoint import EvolveOrchestrator, EvolveProjector
from vibesys.loops.plain.entrypoint import PlainOrchestrator, PlainProjector
from vibesys.orchestration.contracts import OrchestrationRegistry


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register each strategy by ID, execution class, and read projector."""
    registry = OrchestrationRegistry()
    for kind, orchestrator in (
        ("multi-agent", MultiAgentOrchestrator),
        ("single-agent", SingleAgentOrchestrator),
        ("profile-guided-multi-agent", ProfileGuidedMultiAgentOrchestrator),
        ("profile-guided-single-agent", ProfileGuidedSingleAgentOrchestrator),
    ):
        registry.register(
            kind,
            orchestrator,
            projector=AgentProjector(kind),
            portable_namespaces=("agent",),
        )
    registry.register(
        "plain",
        PlainOrchestrator,
        projector=PlainProjector(),
        portable_namespaces=("plain",),
    )
    registry.register(
        "evolve",
        EvolveOrchestrator,
        projector=EvolveProjector(),
        portable_namespaces=("evolve",),
    )
    return registry
