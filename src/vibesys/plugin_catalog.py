"""Product catalog for the built-in orchestration plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.contracts import OrchestrationRegistry
from vibesys.orchestration.evolve.plugin import PLUGIN as EVOLVE_PLUGIN
from vibesys.orchestration.issue_queue.plugin import PLUGIN as ISSUE_QUEUE_PLUGIN
from vibesys.orchestration.multi.plugin import PLUGIN as MULTI_PLUGIN
from vibesys.orchestration.multi.plugin import PROFILE_GUIDED_PLUGIN as PROFILE_MULTI_PLUGIN
from vibesys.orchestration.multi.stub import scripted_response as scripted_multi_response
from vibesys.orchestration.single.plugin import PLUGIN as SINGLE_PLUGIN
from vibesys.orchestration.single.plugin import PROFILE_GUIDED_PLUGIN as PROFILE_SINGLE_PLUGIN
from vibesys.orchestration.single.stub import scripted_response as scripted_single_response

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

type _StubResponseFactory = Callable[[type[BaseModel], int], BaseModel | None]


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


def stub_response_factory(orchestration_id: str) -> _StubResponseFactory | None:
    """Return the selected built-in policy's deterministic stub replies."""
    if orchestration_id in {SINGLE_PLUGIN.id, PROFILE_SINGLE_PLUGIN.id}:
        return scripted_single_response
    if orchestration_id in {MULTI_PLUGIN.id, PROFILE_MULTI_PLUGIN.id}:
        return scripted_multi_response
    return None


__all__ = ["built_in_orchestrations", "stub_response_factory"]
