"""Built-in launch wiring with explicit Fake implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from launch.agents import BuiltInSessionAgents
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from vs_agent.api import AgentEventSink, AgentSpec


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
        provider=spec.provider,
        model=spec.model,
        capabilities=AgentCapabilities(tool_servers=True, session_reuse=True),
        event_sink=events,
    )
    client.set_text(None, "Stub agent inspected the available experiment trajectory.")
    return client


__all__ = ["FakeSessionAgents"]
