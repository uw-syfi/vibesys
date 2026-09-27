"""Product catalog for the built-in orchestration plugins."""

from __future__ import annotations

from vibesys.orchestration.contracts import OrchestrationRegistry
from vibesys.orchestration.evolve.plugin import PLUGIN as EVOLVE_PLUGIN
from vibesys.orchestration.issue_queue.plugin import PLUGIN as ISSUE_QUEUE_PLUGIN
from vibesys.orchestration.multi.plugin import PLUGIN as MULTI_PLUGIN
from vibesys.orchestration.multi.plugin import PROFILE_GUIDED_PLUGIN as PROFILE_MULTI_PLUGIN
from vibesys.orchestration.single.plugin import PLUGIN as SINGLE_PLUGIN
from vibesys.orchestration.single.plugin import PROFILE_GUIDED_PLUGIN as PROFILE_SINGLE_PLUGIN


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register every in-repository policy plugin."""
    registry = OrchestrationRegistry()
    registry.register_plugin(SINGLE_PLUGIN, state_family="agent")
    registry.register_plugin(PROFILE_SINGLE_PLUGIN, state_family="agent")
    registry.register_plugin(MULTI_PLUGIN, state_family="agent")
    registry.register_plugin(PROFILE_MULTI_PLUGIN, state_family="agent")
    registry.register_plugin(ISSUE_QUEUE_PLUGIN)
    registry.register_plugin(EVOLVE_PLUGIN)
    return registry


__all__ = ["built_in_orchestrations"]
