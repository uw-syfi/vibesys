"""A durable session world whose provider is a real stream transport in a container.

``open_stream_host`` builds the pieces :mod:`tests.support.session_world` builds with the
fake provider, but the agent is the production ``ConfinedSessionLauncher`` over agentshim's stream
transports, in a container session. The process it spawns is the scripted far end of
:func:`vs_agent.api.testing.stream_peers`, wrapped by ``FaultyExecutor`` so a fault plan can kill,
silence or corrupt it, or replace its container. The clock and ids are fakes: a hung
turn times out on virtual time, never by waiting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from tests.support.fake_docker_sandbox import FakeDockerSandbox
from tests.support.session_world import (
    FakeSessionResolver,
    ProviderFaults,
    SessionHost,
)

from vs_agent.api import AgentClient, AgentExecutionPolicy, AgentSessionSpec
from vs_agent.api.testing import FakeAgentInvocationStore, FakeExecutor, fake_stream_launcher
from vs_faults.api import FaultPlan, FaultyExecutor

if TYPE_CHECKING:
    from vs_agent.api.testing import StreamPeers
    from vs_core.api import TurnSpec
    from vs_runtime.api.core import AccessGuardedWorkspace
    from vs_sandbox.api import DockerSandbox


@dataclass
class ContainerResolver(FakeSessionResolver):
    """Resolves turns to a container session of ``provider``."""

    provider: str = "claude"

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The provider's container session configuration."""
        return AgentSessionSpec(
            role=turn.session.role_id.root,
            provider=self.provider,
            workspace=workspace.path,
            policy=AgentExecutionPolicy(containerized=True, require_enforcement=False),
        )


@dataclass
class StreamHost:
    """A session host over a stream transport, and the faulty executor under it."""

    host: SessionHost
    executor: FaultyExecutor
    peers: StreamPeers


def open_stream_host(
    provider: str,
    peers: StreamPeers,
    resolver: FakeSessionResolver,
    plan: FaultPlan | None = None,
) -> StreamHost:
    """A host whose provider is ``provider``'s stream transport against ``peers``, faulted by ``plan``.

    The container's workspace is the resolver's, and each role it declares runs in it.
    """
    executor = FaultyExecutor(
        FakeExecutor([], peers=peers.build),
        plan or FaultPlan(seed=0),
        on_container_replaced=peers.forget_conversations,
    )
    sandbox = cast("DockerSandbox", FakeDockerSandbox(workspace=resolver.workspace))
    launcher = fake_stream_launcher(
        provider=provider,
        executor=executor,
        sandboxes=dict.fromkeys((role.root for role in resolver.roles), sandbox),
    )
    client = AgentClient(launcher, provider=provider, containerized=True)
    host = SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())
    return StreamHost(host, executor, peers)
