"""Orchestration runner, independent of built-in implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._orchestrations.runtime import _LocalVibeSysRuntime

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api._orchestrations.contracts import OrchestrationRegistry
    from vibesys.api.contracts import AgentEnvironment, RunRequest
    from vibesys.run.integration import LocalRunIntegration


def run_orchestration(
    request: RunRequest,
    integration: LocalRunIntegration,
    registry: OrchestrationRegistry,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
) -> bool:
    """Execute the selected implementation through the shared contract."""
    with _LocalVibeSysRuntime(
        request, integration, open_agent_environment=open_agent_environment
    ) as runtime:
        if request.orchestration is not None:
            runtime.prepare()
        return registry.resolve(request.orchestration_id).execute(request, runtime)
