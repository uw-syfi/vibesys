"""Composition point for built-in orchestration implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.entrypoint import AgentOrchestration
from vibesys.loops.agent.profile_entrypoint import ProfileGuidedOrchestration
from vibesys.loops.evolve.entrypoint import EvolveOrchestration
from vibesys.loops.legacy_request import LoopKind
from vibesys.loops.plain.entrypoint import PlainOrchestration
from vibesys.orchestration.contracts import OrchestrationRegistry

if TYPE_CHECKING:
    from vibesys.orchestration import ResumeProjection
    from vs_project.api import OrchestrationRunManifest


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register each current loop ID with its own concrete implementation."""
    registry = OrchestrationRegistry()
    registry.register(LoopKind.AGENT, AgentOrchestration())
    registry.register(LoopKind.PROFILE_GUIDED, ProfileGuidedOrchestration())
    registry.register(LoopKind.PLAIN, PlainOrchestration())
    registry.register(LoopKind.EVOLVE, EvolveOrchestration())
    return registry


def resume_projection(manifest: OrchestrationRunManifest) -> ResumeProjection:
    """Delegate descriptor validation and CLI projection to its owner."""
    return built_in_orchestrations().resolve(manifest.orchestration.id).resume_projection(manifest)
