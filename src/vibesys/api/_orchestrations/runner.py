"""Orchestration runner, independent of built-in implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.api._orchestrations.contracts import OrchestrationRegistry
    from vibesys.api.contracts import RunRequest
    from vibesys.run.integration import LocalRunIntegration


def run_orchestration(
    request: RunRequest,
    integration: LocalRunIntegration,
    registry: OrchestrationRegistry,
) -> bool:
    """Execute the selected implementation through the shared contract."""
    return registry.resolve(request.loop).execute(request, integration)
