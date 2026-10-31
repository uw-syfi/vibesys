"""Open the product's core host over a fake container backend, for tests of what it composes.

The host is the one a launched run uses. A test that exercises the workspaces or the local
evaluation of a run, not a strategy, opens it with a stub plugin and a client that never
takes a turn, and reads what the host composed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from tests.support.core_plugins import EmptyOptions, capability_plugin
from tests.support.docker_environment import host_container_backend

from vibesys.api.testing import FakeProfilerPreflight
from vibesys.run.host import open_product_core_host
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_runtime.api.core import RunTiming
from vs_runtime.api.testing import FakeStopTimer
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from vibesys.run.contracts import RunRequest
    from vibesys.run.host import CoreHost
    from vibesys.run.integration import LocalRunIntegration
    from vs_runtime.api import OrchestrationPlugin
    from vs_sandbox.api import ComputeBackendImpl


def _resumable_client(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


@asynccontextmanager
async def open_core_host(
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    plugin: OrchestrationPlugin | None = None,
    backend_factory: Callable[..., ComputeBackendImpl] = host_container_backend,
) -> AsyncIterator[CoreHost]:
    """Open the core host of ``request`` on the real event loop, closing it on exit."""
    async with open_product_core_host(
        request,
        integration,
        plugin=plugin or capability_plugin(request.orchestration.id),
        options=EmptyOptions(),
        timing=RunTiming(integration.clock, OsThreads()),
        agent_client_factory=_resumable_client,
        backend_factory=backend_factory,
        invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
        stop_timer=FakeStopTimer(threads=OsThreads()),
        blocking=ThreadBlockingRunner(),
        network=UnixNetwork(),
        profiler_preflight=FakeProfilerPreflight(),
    ) as host:
        yield host


__all__ = ["open_core_host"]
