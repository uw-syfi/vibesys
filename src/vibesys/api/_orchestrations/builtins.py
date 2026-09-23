"""Composition point for built-in orchestration adapters."""

from __future__ import annotations

from vibesys.api._orchestrations.agent import AgentOrchestration
from vibesys.api._orchestrations.contracts import OrchestrationRegistry
from vibesys.api._orchestrations.evolve import EvolveOrchestration
from vibesys.api._orchestrations.plain import PlainOrchestration
from vibesys.api.contracts import LoopKind


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register current public loop IDs, including the agent variant."""
    registry = OrchestrationRegistry()
    agent = AgentOrchestration()
    registry.register(LoopKind.AGENT, agent)
    registry.register(LoopKind.PROFILE_GUIDED, agent)
    registry.register(LoopKind.PLAIN, PlainOrchestration())
    registry.register(LoopKind.EVOLVE, EvolveOrchestration())
    return registry
