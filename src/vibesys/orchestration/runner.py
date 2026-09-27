"""Run one descriptor-validated orchestration in the shared host."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.runtime import RunContext

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.orchestration.contracts import (
        OrchestrationProjector,
        PreparedPlugin,
    )
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol, ToolServerDescriptor
    from vs_runtime.api import RunStatus, Workspace
    from vs_sandbox.api import ComputeBackendImpl


async def run_plugin(  # noqa: PLR0913  # LW-040002 [PLR0913]; injected runtime collaborators retain independent ownership.
    request: RunRequest,
    integration: LocalRunIntegration,
    prepared: PreparedPlugin,
    *,
    open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    projector: OrchestrationProjector | None = None,
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    agent_tool_bindings: Mapping[
        str, Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]
    ]
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
        agent_tool_bindings=agent_tool_bindings,
        plugin=plugin,
    ) as ctx:
        return await plugin.orchestrate(ctx, prepared.options)
