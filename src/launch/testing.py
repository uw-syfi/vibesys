"""Built-in launch wiring with explicit Fake implementations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from launch import LaunchSettings
from launch import create_session as _create_session
from launch.agents import BuiltInSessionAgents
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api.testing import FakeStopTimer

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api import OrchestrationRegistry, RunRequest, RunSession
    from vibesys.api.contracts import EventSink
    from vs_agent.api import AgentClientProtocol, AgentEventSink, AgentSpec
    from vs_runtime.api.infrastructure import StopTimer
    from vs_sandbox.api import ComputeBackendImpl


# lint-waiver: LW-125701 [PLR0913]; a settings bundle would hide the individual
# > Fake replacements in existing callers; merging their factories would couple
# > independently owned libraries.
def create_session(  # noqa: PLR0913
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry,
    agent_client_factory: Callable[..., AgentClientProtocol],
    backend_factory: Callable[..., ComputeBackendImpl],
    stop_timer: StopTimer = asyncio.sleep,
) -> RunSession:
    """Execute real built-in wiring with caller-owned Fake implementations."""
    return _create_session(
        request,
        sink=sink,
        settings=LaunchSettings(registry, agent_client_factory, backend_factory, stop_timer),
    )


class FakeSessionAgents(BuiltInSessionAgents):
    """Run-scoped agent wiring with in-memory conversations.

    Validation, environment grants and cleanup use the same assembly as the
    built-in implementation. Only the external agent execution is replaced.
    """

    def __init__(self) -> None:
        """Create a fresh Fake agent client for each auxiliary conversation."""
        super().__init__(client_factory=_fake_client)


def _fake_client(*, spec: AgentSpec, events: AgentEventSink, **_kwargs: object) -> FakeAgentClient:
    client = FakeAgentClient(
        backend_name=spec.backend.value,
        driver_name=spec.driver.value,
        provider=spec.provider,
        model=spec.model,
        capabilities=AgentCapabilities(tool_servers=True, session_reuse=True),
        event_sink=events,
    )
    client.set_text(None, "Stub agent inspected the available experiment trajectory.")
    return client


__all__ = ["FakeSessionAgents", "FakeStopTimer", "create_session"]
