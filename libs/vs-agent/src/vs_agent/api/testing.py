"""The owned fake/test-double surface for ``vs_agent``.

Tests import doubles from here rather than reaching into the library's
internal modules directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_agent.drivers.agentshim import AgentShimDriver
from vs_agent.drivers.fake import FakeDriver
from vs_agent.fake_client import FakeAgentClient, FakeInvocation

if TYPE_CHECKING:
    import agentshim
    from agentshim.testing import FakeExecutor

    from vs_agent.contracts import AgentDriver, AgentSessionSpec

__all__ = [
    "FakeAgentClient",
    "FakeDriver",
    "FakeInvocation",
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
