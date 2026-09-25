"""Composition point for concrete built-in orchestrators."""

from __future__ import annotations

from vibesys.loops.evolve.entrypoint import EvolveOrchestrator, EvolveProjector
from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator, IssueQueueProjector
from vibesys.loops.multi.orchestration import (
    MultiAgentOrchestrator,
    MultiProjector,
    ProfileGuidedMultiAgentOrchestrator,
    ProfileMultiProjector,
)
from vibesys.loops.profile_single.orchestration import (
    ProfileGuidedSingleAgentOrchestrator,
    ProfileSingleProjector,
)
from vibesys.loops.single.orchestration import SingleAgentOrchestrator, SingleProjector
from vibesys.orchestration.contracts import OrchestrationRegistry


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register each strategy by ID, execution class, and read projector."""
    registry = OrchestrationRegistry()
    for kind, namespace, orchestrator, projector in (
        ("multi-agent", "multi", MultiAgentOrchestrator, MultiProjector()),
        ("single-agent", "single", SingleAgentOrchestrator, SingleProjector()),
        (
            "profile-guided-multi-agent",
            "profile_multi",
            ProfileGuidedMultiAgentOrchestrator,
            ProfileMultiProjector(),
        ),
        (
            "profile-guided-single-agent",
            "profile_single",
            ProfileGuidedSingleAgentOrchestrator,
            ProfileSingleProjector(),
        ),
    ):
        registry.register(
            kind,
            orchestrator,
            projector=projector,
            portable_namespaces=(namespace,),
            state_family="agent",
        )
    registry.register(
        "plain",
        IssueQueueOrchestrator,
        projector=IssueQueueProjector(),
        portable_namespaces=("plain",),
    )
    registry.register(
        "evolve",
        EvolveOrchestrator,
        projector=EvolveProjector(),
        portable_namespaces=("evolve",),
    )
    return registry
