"""Test-only execution seam for the retired loop implementations.

Golden and crash/resume scenarios continue to exercise their historical
policies until each scenario is ported to its catalog plugin. Delete this
helper and ``src/vibesys/loops`` together once those scenarios have plugin
equivalents; no production module may import either in the interim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vibesys.orchestration.runtime import RunContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.context import RunSetup
    from vibesys.orchestration.contracts import OrchestrationProjector
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.gates import GateExecutor
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol
    from vs_project.api import OrchestrationDescriptor


class LegacyOrchestrator(Protocol):
    """The old loop shape retained only for its migration tests."""

    setup: RunSetup

    def __init__(self, descriptor: OrchestrationDescriptor) -> None: ...

    async def run(self, ctx: RunContext) -> bool: ...


async def run_orchestration(  # noqa: PLR0913  # test-isolation: temporary legacy golden/resume harness retains independent fake seams.
    request: RunRequest,
    integration: LocalRunIntegration,
    orchestrator: LegacyOrchestrator,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    projector: OrchestrationProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    gate_executor: GateExecutor | None = None,
) -> bool:
    """Drive one historical policy without restoring production dispatch."""
    async with RunContext.open(
        request,
        integration,
        setup=orchestrator.setup,
        open_agent_environment=open_agent_environment,
        projector=projector,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        gate_executor=gate_executor,
    ) as ctx:
        return await orchestrator.run(ctx)
