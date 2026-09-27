"""Thin product catalog for the built-in orchestration registrations."""

from vibesys.orchestration.evolve import REGISTRATION as EVOLVE
from vibesys.orchestration.issue_queue import REGISTRATION as ISSUE_QUEUE
from vibesys.orchestration.multi import (
    PROFILE_GUIDED_REGISTRATION as PROFILE_GUIDED_MULTI,
)
from vibesys.orchestration.multi import REGISTRATION as MULTI
from vibesys.orchestration.single import (
    PROFILE_GUIDED_REGISTRATION as PROFILE_GUIDED_SINGLE,
)
from vibesys.orchestration.single import REGISTRATION as SINGLE
from vibesys.plugin_catalog import OrchestrationRegistry


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register every in-repository orchestration policy."""
    registry = OrchestrationRegistry()
    for registration in (
        SINGLE,
        PROFILE_GUIDED_SINGLE,
        MULTI,
        PROFILE_GUIDED_MULTI,
        ISSUE_QUEUE,
        EVOLVE,
    ):
        registry.register(registration)
    return registry


__all__ = ["built_in_orchestrations"]
