"""Run one descriptor-validated orchestration in the shared host."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.runtime import RunContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.orchestration.contracts import Orchestrator
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol


async def run_orchestration(  # noqa: PLR0913  # tracked: #288
    request: RunRequest,
    integration: LocalRunIntegration,
    orchestrator: Orchestrator,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
) -> bool:
    """Open the run host and invoke the selected policy once.

    *agent_client_factory* and *backend_factory* are injection seams: a test
    passes a fake in place of the real agent client / compute backend
    construction (`vs_agent.api.build_agent_client`, `vibesys.backends.get`).
    Both default to the real implementation, so production callers are
    unaffected.
    """
    async with RunContext.open(
        request,
        integration,
        setup=orchestrator.setup,
        open_agent_environment=open_agent_environment,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
    ) as ctx:
        return await orchestrator.run(ctx)
