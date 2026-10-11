"""Thin product catalog for the built-in orchestration registrations."""

from vibesys.api import IterationSupport, OrchestrationRegistry
from vibesys.api.catalog import BUILT_INS


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register every in-repository orchestration policy."""
    registry = OrchestrationRegistry()
    for registration in BUILT_INS:
        if registration.iteration_support is IterationSupport.UNMAPPED:
            message = (
                f"built-in orchestration {registration.plugin.id!r} must declare "
                "its iteration navigation disposition"
            )
            raise ValueError(message)
        registry.register(registration)
    return registry


__all__ = ["built_in_orchestrations"]
