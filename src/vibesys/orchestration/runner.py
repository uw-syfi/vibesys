"""Run one descriptor-validated orchestration in the shared host."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.runtime import RunContext

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.orchestration.contracts import (
        OrchestrationProjector,
        Orchestrator,
        PreparedPlugin,
    )
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.gates import GateExecutor
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol, MCPServerSpec
    from vs_runtime.api import RunStatus, Workspace


async def run_orchestration(  # noqa: PLR0913  # LW-040002 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    request: RunRequest,
    integration: LocalRunIntegration,
    orchestrator: Orchestrator,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    projector: OrchestrationProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    gate_executor: GateExecutor | None = None,
) -> bool:
    """Open the run host and invoke the selected policy once.

    *projector* is the policy's registered read projection, when one is
    known (`vibesys.api.session` resolves it from the same registration as
    *orchestrator*). The host uses it to derive round/experiment events from
    committed state (`RunContext.state.commit`); without it, that derivation
    is inert and a caller who never checkpoints through `commit` is
    unaffected either way.

    *agent_client_factory* and *backend_factory* are injection seams: a test
    passes a fake in place of the real agent client / compute backend
    construction (`vs_agent.api.build_agent_client`, `vibesys.backends.get`).
    *gate_executor* is the same kind of seam for `ctx.gates`' trusted
    accuracy/benchmark commands. All three default to the real
    implementation, so production callers are unaffected.
    """
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


async def run_plugin(  # noqa: PLR0913  # LW-040002 [PLR0913]; injected runtime collaborators retain independent ownership.
    request: RunRequest,
    integration: LocalRunIntegration,
    prepared: PreparedPlugin,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    projector: OrchestrationProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    gate_executor: GateExecutor | None = None,
    agent_tool_bindings: Mapping[str, Callable[[object, Workspace], tuple[MCPServerSpec, ...]]]
    | None = None,
) -> RunStatus:
    """Open the runtime adapter and invoke one prepared plugin."""
    plugin = prepared.plugin
    async with RunContext.open(
        request,
        integration,
        setup=prepared.setup,
        open_agent_environment=open_agent_environment,
        projector=projector,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        gate_executor=gate_executor,
        agent_tool_bindings=agent_tool_bindings,
        plugin=plugin,
    ) as ctx:
        return await plugin.orchestrate(ctx, prepared.options)
