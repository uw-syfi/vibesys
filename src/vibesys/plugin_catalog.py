"""Product registry for orchestration plugin registrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from vibesys.orchestration.registration import (
    OrchestrationProjector,
    OrchestrationRegistration,
    project_run,
)
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from vs_runtime.api import OrchestrationPlugin


class OrchestrationRegistry:
    """Map stable orchestration IDs to product registrations."""

    def __init__(self) -> None:
        """Create an empty registration table."""
        self._registrations: dict[str, OrchestrationRegistration] = {}

    def register_plugin(
        self,
        plugin: OrchestrationPlugin,
    ) -> None:
        """Register a runtime plugin without optional VibeSys product hooks."""
        self.register(OrchestrationRegistration(plugin=plugin))

    def register(self, registration: OrchestrationRegistration) -> None:
        """Register one product-owned binding for a runtime plugin."""
        plugin = registration.plugin
        try:
            OrchestrationDescriptor(id=plugin.id, config_version=plugin.config_version, options={})
        except ValidationError as exc:
            message = f"invalid orchestration plugin ID {plugin.id!r}"
            raise ValueError(message) from exc
        if plugin.id in self._registrations:
            message = f"orchestration {plugin.id!r} is already registered"
            raise ValueError(message)
        self._registrations[plugin.id] = registration

    def resolve(self, kind: str) -> OrchestrationRegistration:
        """Return the registration or reject an unknown orchestration ID."""
        try:
            return self._registrations[kind]
        except KeyError as exc:
            message = f"orchestration {kind!r} is not registered"
            raise ValueError(message) from exc


__all__ = [
    "OrchestrationProjector",
    "OrchestrationRegistration",
    "OrchestrationRegistry",
    "project_run",
]
