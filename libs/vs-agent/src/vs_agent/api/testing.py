"""The owned fake/test-double surface for ``vs_agent``.

Tests import doubles from here rather than reaching into the library's
internal modules directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_agent.drivers.agentshim import AgentShimDriver
from vs_agent.drivers.fake import FakeDriver, FakeTurnScript
from vs_agent.fake_client import FakeAgentClient, FakeInvocation
from vs_agent.sessions import AgentInvocationState, ClientAgentSessions

if TYPE_CHECKING:
    import agentshim
    from agentshim.testing import FakeExecutor

    from vs_agent.contracts import AgentDriver, AgentSessionSpec
    from vs_agent.sessions import AgentInvocationStore, AgentTurnExecutor

__all__ = [
    "FakeAgentClient",
    "FakeAgentInvocationStore",
    "FakeAgentSessions",
    "FakeDriver",
    "FakeInvocation",
    "FakeTurnScript",
    "fake_agentshim_driver",
]


def fake_agentshim_driver(*, provider: str, executor: FakeExecutor) -> AgentDriver:
    """Drive the real usage/policy adapter with a scripted in-memory executor.

    No provider CLI or operator environment is used. The fake executor emits
    the provider's real protocol; normalization follows the production path.
    No process runs, so workspace confinement is satisfied vacuously.
    """
    return _FakeAgentShimDriver(
        provider=provider,
        executor_factory=lambda: executor,
        launcher_env=dict,
        transient_retry_delays=(),
    )


class _FakeAgentShimDriver(AgentShimDriver):
    """Keep policy/usage translation while replacing host setup with memory."""

    def _sandbox_for(
        self,
        spec: AgentSessionSpec,
        config_scope: agentshim.ConfigScope,
    ) -> tuple[None, None, dict[str, str]]:
        # The executor touches no host paths and resolves its own fake binary.
        # Building a real host sandbox would reintroduce I/O into this fake.
        del config_scope
        return None, None, dict(spec.environment)


class FakeAgentSessions(ClientAgentSessions):
    """In-memory agent execution with the production durable dispatch guarantees.

    The caller supplies an AgentClient over FakeDriver or a faithful executor.
    Reusing the dispatch mechanism keeps the Fake's identity, failure and crash
    semantics identical to production; its shared ledger retains crash evidence.
    """

    def __init__(self, client: AgentTurnExecutor, slot: AgentInvocationStore | None = None) -> None:
        """Bind fake external execution to the same strict invocation ledger."""
        super().__init__(client, slot if slot is not None else FakeAgentInvocationStore())


class FakeAgentInvocationStore:
    """In-memory persistence with production schema validation and crash retention."""

    def __init__(self) -> None:
        """Create an absent ledger, share this instance across reconstructions."""
        self._document: str | None = None

    def load_optional(self) -> AgentInvocationState | None:
        """Round-trip the stored external contract on every read."""
        return (
            None
            if self._document is None
            else AgentInvocationState.model_validate_json(self._document)
        )

    def save(self, model: AgentInvocationState) -> None:
        """Validate and atomically replace the persisted document."""
        document = model.model_dump_json()
        AgentInvocationState.model_validate_json(document)
        self._document = document
