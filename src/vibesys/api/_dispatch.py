"""Select an orchestration without knowing its internal execution structure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration._common import resolved_run_id as _resolved_run_id
from vibesys.orchestration.runner import run_orchestration

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.contracts import AgentEnvironment
    from vibesys.api.run_request import RunRequestLike
    from vibesys.orchestration.contracts import Orchestration, OrchestrationRegistry
    from vibesys.run.integration import LocalRunIntegration

resolved_run_id = _resolved_run_id


def dispatch_loop(
    request: RunRequestLike,
    integration: LocalRunIntegration,
    registry: OrchestrationRegistry | None = None,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    implementation: Orchestration | None = None,
) -> bool:
    """Execute the registered orchestration for this request's stable loop ID."""
    if registry is None:
        from vibesys.loops.registry import built_in_orchestrations  # noqa: PLC0415

        registry = built_in_orchestrations()
    return run_orchestration(
        request,
        integration,
        registry,
        open_agent_environment=open_agent_environment,
        implementation=implementation,
    )
