"""Select an orchestration without knowing its internal execution structure."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._orchestrations._common import resolved_run_id as _resolved_run_id
from vibesys.api._orchestrations.builtins import built_in_orchestrations
from vibesys.api._orchestrations.runner import run_orchestration

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api._orchestrations.contracts import Orchestration, OrchestrationRegistry
    from vibesys.api.contracts import AgentEnvironment, AnyRunRequest
    from vibesys.run.integration import LocalRunIntegration

resolved_run_id = _resolved_run_id


def dispatch_loop(
    request: AnyRunRequest,
    integration: LocalRunIntegration,
    registry: OrchestrationRegistry | None = None,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    implementation: Orchestration | None = None,
) -> bool:
    """Execute the registered orchestration for this request's stable loop ID."""
    return run_orchestration(
        request,
        integration,
        registry or built_in_orchestrations(),
        open_agent_environment=open_agent_environment,
        implementation=implementation,
    )
