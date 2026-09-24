"""Composition point for built-in orchestration implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._orchestrations.agent import AgentOrchestration
from vibesys.api._orchestrations.contracts import OrchestrationRegistry
from vibesys.api._orchestrations.evolve import EvolveOrchestration
from vibesys.api._orchestrations.legacy_request import LoopKind
from vibesys.api._orchestrations.plain import PlainOrchestration
from vibesys.api._orchestrations.profile_guided import ProfileGuidedOrchestration

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
