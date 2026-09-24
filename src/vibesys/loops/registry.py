"""Composition point for concrete built-in orchestrators."""

from __future__ import annotations

from vibesys.loops.evolve.entrypoint import EvolveOrchestrator, EvolveProjector
from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator, IssueQueueProjector
from vibesys.loops.multi.entrypoint import MultiAgentOrchestrator
from vibesys.loops.multi.projection import MultiProjector
from vibesys.loops.profile_multi.entrypoint import ProfileGuidedMultiAgentOrchestrator
from vibesys.loops.profile_multi.projection import ProfileMultiProjector
from vibesys.loops.profile_single.entrypoint import ProfileGuidedSingleAgentOrchestrator
from vibesys.loops.profile_single.projection import ProfileSingleProjector
from vibesys.loops.single.entrypoint import SingleAgentOrchestrator
from vibesys.loops.single.projection import SingleProjector
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
