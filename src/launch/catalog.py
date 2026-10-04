"""Thin product catalog for the built-in orchestration registrations."""

from vibesys.api import OrchestrationRegistry
from vibesys.api.catalog import (
    DYNAMIC,
    EVOLVE,
    ISSUE_QUEUE,
    MULTI,
    PROFILE_GUIDED_MULTI,
    PROFILE_GUIDED_SINGLE,
    SINGLE,
)


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register every in-repository orchestration policy."""
    registry = OrchestrationRegistry()
    for registration in (
        SINGLE,
        DYNAMIC,
        PROFILE_GUIDED_SINGLE,
        MULTI,
        PROFILE_GUIDED_MULTI,
        ISSUE_QUEUE,
        EVOLVE,
    ):
        registry.register(registration)
    return registry


__all__ = ["built_in_orchestrations"]
