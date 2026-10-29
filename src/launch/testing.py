"""Built-in launch wiring with explicit Fake implementations."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from launch.agents import BuiltInSessionAgents
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from vibesys.api import OrchestrationRegistry
    from vs_agent.api import AgentEventSink, AgentSpec
    from vs_project.api import Git
    from vs_sim.api import Network, Threads


class FakeSessionAgents(BuiltInSessionAgents):
    """Run-scoped agent wiring with in-memory conversations.

    Validation, environment grants and cleanup use the same assembly as the
    built-in implementation. Only the external agent execution is replaced.
    """

    def __init__(
        self,
        threads: Threads,
        *,
        network: Network,
        repository: Git,
        registry: OrchestrationRegistry,
    ) -> None:
        """Create a fresh Fake agent client for each auxiliary conversation.

        ``network``, ``repository`` and ``registry`` serve and read the run tools as in
        production; a test passes Fakes for the first two.
        """
        super().__init__(
            client_factory=partial(_fake_client, threads=threads),
            network=network,
            git=repository,
            registry=registry,
        )


def _fake_client(
    *, spec: AgentSpec, events: AgentEventSink, threads: Threads, **_kwargs: object
) -> FakeAgentClient:
    client = FakeAgentClient(
        threads=threads,
        backend_name=spec.backend.value,
        provider=spec.provider,
        model=spec.model,
        capabilities=AgentCapabilities(tool_servers=True, session_reuse=True),
        event_sink=events,
    )
    client.set_text(None, "Stub agent inspected the available experiment trajectory.")
    return client


__all__ = ["FakeSessionAgents"]
