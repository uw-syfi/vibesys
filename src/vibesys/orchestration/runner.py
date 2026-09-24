"""Orchestration runner, independent of built-in implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.runtime import _LocalVibeSysRuntime

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.contracts import AgentEnvironment
    from vibesys.api.run_request import RunRequestLike
    from vibesys.orchestration.contracts import Orchestration, OrchestrationRegistry
    from vibesys.run.integration import LocalRunIntegration


def run_orchestration(
    request: RunRequestLike,
    integration: LocalRunIntegration,
    registry: OrchestrationRegistry,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    implementation: Orchestration | None = None,
) -> bool:
    """Execute the selected implementation through the shared contract."""
    policy = implementation or registry.resolve(request.orchestration_id)
    with _LocalVibeSysRuntime(
        request, integration, open_agent_environment=open_agent_environment
    ) as runtime:
        runtime.prepare()
        policy.prepare(request, runtime)
        return policy.execute(request, runtime)
