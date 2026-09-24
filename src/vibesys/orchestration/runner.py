"""Run one descriptor-validated orchestration in the shared host."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.runtime import RunContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.orchestration.contracts import OrchestrationProjector, Orchestrator
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration


async def run_orchestration(
    request: RunRequest,
    integration: LocalRunIntegration,
    orchestrator: Orchestrator,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    projector: OrchestrationProjector | None = None,
) -> bool:
    """Open the run host and invoke the selected policy once.

    *projector* is the policy's registered read projection, when one is
    known (`vibesys.api.session` resolves it from the same registration as
    *orchestrator*). The host uses it to derive round/experiment events from
    committed state (`RunContext.state.commit`); without it, that derivation
    is inert and a caller who never checkpoints through `commit` is
    unaffected either way.
    """
    async with RunContext.open(
        request,
        integration,
        setup=orchestrator.setup,
        open_agent_environment=open_agent_environment,
        projector=projector,
    ) as ctx:
        return await orchestrator.run(ctx)
