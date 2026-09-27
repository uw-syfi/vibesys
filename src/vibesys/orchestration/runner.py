"""Run one descriptor-validated orchestration in the shared host."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.run.host import open_product_run_host

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vibesys.orchestration.contracts import OrchestrationProjector
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol, ToolServerDescriptor
    from vs_runtime.api import OrchestrationPlugin, RunStatus, Workspace
    from vs_sandbox.api import ComputeBackendImpl


async def run_plugin(  # noqa: PLR0913  # LW-040002 [PLR0913]; injected runtime collaborators retain independent ownership.
    request: RunRequest,
    integration: LocalRunIntegration,
    plugin: OrchestrationPlugin,
    options: BaseModel,
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
    async with open_product_run_host(
        request,
        integration,
        open_agent_environment=open_agent_environment,
        projector=projector,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
        agent_tool_bindings=agent_tool_bindings,
        plugin=plugin,
    ) as ctx:
        return await plugin.orchestrate(ctx, options)
