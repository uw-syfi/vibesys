"""The owned fake/test-double surface for ``vs_agent``.

Tests import doubles from here rather than reaching into the library's
internal modules directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentshim
from agentshim.testing import (
    FakeClock,
    FakeExecutor,
    FakeRun,
    SequentialIds,
    TokenUsage,
    installed_mcp_servers,
    scripted_turn,
)

from vs_agent import fake_profiles
from vs_agent.fake_client import FakeAgentClient, FakeInvocation
from vs_agent.fake_docker_build_runner import (
    DockerResult,
    FakeDockerBuildRunner,
    docker_result,
    docker_timed_out,
)
from vs_agent.fault_injection import (
    KILLED_STATUS,
    MALFORMED_LINE,
    NOT_A_REPLY,
    TURN_BUDGET_S,
    CommandExecutor,
    ConversationFaultKind,
    FaultingExecutor,
    FaultingTransport,
    ProcessFaultKind,
    Transport,
)
from vs_agent.scripted_provider import (
    FakeProvider,
    FakeProviderError,
    FakeTurnScript,
    HandSession,
    assistant_text,
    thinking,
    todo_write,
    tool_call,
    tool_result,
    usage,
)
from vs_agent.session_launch import ConfinedSessionLauncher
from vs_agent.sessions import AgentInvocationState, ClientAgentSessions
from vs_agent.stream_peers import StreamPeers, answering_with, stream_peers

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vs_agent.contracts import AgentSessionSpec
    from vs_agent.session_launch import SessionLauncher
    from vs_agent.sessions import AgentInvocationStore, AgentTurnExecutor
    from vs_sandbox.api import DockerSandbox

type CommandRequest = agentshim.CommandRequest
"""The spawn request a ``FakeExecutor`` script receives."""

__all__ = [
    "KILLED_STATUS",
    "MALFORMED_LINE",
    "NOT_A_REPLY",
    "TURN_BUDGET_S",
    "CommandExecutor",
    "CommandRequest",
    "ConversationFaultKind",
    "DockerResult",
    "FakeAgentClient",
    "FakeAgentInvocationStore",
    "FakeAgentSessions",
    "FakeDockerBuildRunner",
    "FakeExecutor",
    "FakeInvocation",
    "FakeProvider",
    "FakeProviderError",
    "FakeRun",
    "FakeTurnScript",
    "FaultingExecutor",
    "FaultingTransport",
    "HandSession",
    "ProcessFaultKind",
    "StreamPeers",
    "TokenUsage",
    "Transport",
    "answering_with",
    "assistant_text",
    "docker_result",
    "docker_timed_out",
    "fake_agentshim_launcher",
    "fake_profiles",
    "fake_stream_launcher",
    "installed_mcp_servers",
    "scripted_turn",
    "stream_peers",
    "thinking",
    "todo_write",
    "tool_call",
    "tool_result",
    "usage",
]


def fake_agentshim_launcher(
    *,
    provider: str,
    executor: FakeExecutor,
    transport: agentshim.TransportKind = agentshim.TransportKind.ONE_SHOT,
) -> SessionLauncher:
    """Launch real sessions under the production policy over a scripted in-memory executor.

    No provider CLI or operator environment is used. The fake executor emits
    the provider's real protocol; normalization follows the production path.
    No process runs, so workspace confinement is satisfied vacuously.
    ``transport`` is the one the scripted executor speaks: a ``STREAM`` fake
    needs an executor built with ``peers``.
    """
    return _FakeConfinedLauncher(
        provider=provider,
        executor_factory=lambda: executor,
        launcher_env=dict,
        transient_retry_delays=(),
        transport=transport,
    )


def fake_stream_launcher(
    *,
    provider: str,
    executor: CommandExecutor,
    sandboxes: Mapping[str, DockerSandbox],
    workspace_sandboxes: Callable[[Path], DockerSandbox | None] | None = None,
) -> SessionLauncher:
    """Launch container sessions over a stream transport whose process is ``executor``.

    The executor is the far end of the provider's long-lived process (for
    example a ``FakeExecutor`` built with ``peers`` from :func:`stream_peers`,
    possibly wrapped by a fault injector). The clock and ids are fakes, so a
    hung turn times out on virtual time and never by waiting.
    """
    return ConfinedSessionLauncher(
        provider=provider,
        docker_sandboxes=dict(sandboxes),
        workspace_sandboxes=workspace_sandboxes,
        executor_factory=lambda: executor,
        launcher_env=dict,
        transient_retry_delays=(),
        clock=FakeClock(),
        ids=SequentialIds(),
    )


class _FakeConfinedLauncher(ConfinedSessionLauncher):
    """Keep the production launch policy while replacing host setup with memory."""

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

    The caller supplies an AgentClient over FakeProvider or a faithful executor.
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
