"""Composition point for built-in orchestration adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from vibesys.api._orchestrations.agent import AgentOrchestration
from vibesys.api._orchestrations.contracts import OrchestrationRegistry
from vibesys.api._orchestrations.evolve import EvolveOrchestration
from vibesys.api._orchestrations.plain import PlainOrchestration
from vibesys.api.contracts import LoopKind

if TYPE_CHECKING:
    from vs_project.api import OrchestrationRunManifest, RunConfiguration


class _LegacyResumeProjector(Protocol):
    def legacy_resume_configuration(
        self, manifest: OrchestrationRunManifest
    ) -> RunConfiguration: ...


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register current public loop IDs, including the agent variant."""
    registry = OrchestrationRegistry()
    agent = AgentOrchestration()
    registry.register(LoopKind.AGENT, agent)
    registry.register(LoopKind.PROFILE_GUIDED, agent)
    registry.register(LoopKind.PLAIN, PlainOrchestration())
    registry.register(LoopKind.EVOLVE, EvolveOrchestration())
    return registry


def legacy_resume_configuration(manifest: OrchestrationRunManifest) -> RunConfiguration:
    """Transitional CLI projection delegated to the owning orchestration adapter."""
    implementation = built_in_orchestrations().resolve(LoopKind(manifest.orchestration.id))
    projector = cast("_LegacyResumeProjector", implementation)
    return projector.legacy_resume_configuration(manifest)
