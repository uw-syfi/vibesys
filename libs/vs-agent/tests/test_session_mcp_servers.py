"""A session's MCP servers are fixed when its conversation opens, for every stream provider.

Codex's app-server refuses a turn that names servers the conversation was not
opened with; Claude's stream-json tolerates it. The scripts below play each
provider's real far end, so the real transports apply their own rule.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import agentshim
from agentshim.testing import (
    ClaudePeerTurn,
    ClaudeStreamPeers,
    CodexScript,
    FakeClock,
    FakeExecutor,
    Say,
    SequentialIds,
)
from hypothesis import given
from hypothesis import strategies as st
from tests.support.fake_docker_sandbox import FakeDockerSandbox

# test-isolation: these tests exercise the launcher's own internals, which the facade deliberately hides
from vs_agent import session_launch as subject
from vs_agent.api import (
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
    MCPServerSpec,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshim.execution.process import SpawnRequest
    from agentshim.testing import FakePeer

    from vs_sandbox.api import DockerSandbox

STREAM_PROVIDERS = tuple(agentshim.stream_provider_names())


def _answering(provider: str, count: int) -> Callable[[SpawnRequest], FakePeer]:
    if provider == "claude":
        return ClaudeStreamPeers([ClaudePeerTurn(text=f"t{i}") for i in range(count)]).build
    script = CodexScript()
    for i in range(count):
        script.turn(Say(f"t{i}"))
    return script.peer


def _run_turns(
    provider: str,
    servers: tuple[MCPServerSpec, ...],
    turns: int,
    peers: Callable[[SpawnRequest], FakePeer] | None = None,
) -> list[str]:
    """Open a containerized session with *servers* and run *turns* turns on it."""
    with TemporaryDirectory() as raw:
        workspace = Path(raw)
        sandbox = FakeDockerSandbox(workspace=workspace)
        launcher = subject.ConfinedSessionLauncher(
            provider=provider,
            docker_sandboxes={"implementer": cast("DockerSandbox", sandbox)},
            executor_factory=lambda: FakeExecutor([], peers=peers or _answering(provider, turns)),
            launcher_env=dict,
            transient_retry_delays=(),
            clock=FakeClock(),
            ids=SequentialIds(),
        )
        session = launcher.launch(
            AgentSessionSpec(
                role="implementer",
                provider=provider,
                workspace=workspace,
                model="test-model",
                policy=AgentExecutionPolicy(containerized=True),
                mcp_servers=servers,
            )
        )
        return [session.run_turn(AgentTurnRequest(message=f"go {i}")).text for i in range(turns)]


_names = st.lists(
    st.text("abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6), unique=True, max_size=3
)


def _server(name: str, arg: str) -> MCPServerSpec:
    return MCPServerSpec(name=name, command="python", args=("-m", f"srv_{arg}", "/run/srv.sock"))


@given(
    provider=st.sampled_from(STREAM_PROVIDERS),
    names=_names,
    turns=st.integers(min_value=1, max_value=3),
)
def test_turns_run_on_a_session_opened_with_any_server_set(
    provider: str, names: list[str], turns: int
) -> None:
    servers = tuple(_server(name, name) for name in names)

    assert _run_turns(provider, servers, turns) == [f"t{i}" for i in range(turns)]


def test_a_codex_session_with_a_tool_server_runs_its_turns() -> None:
    servers = (_server("vibesys_issues", "issues"),)

    assert _run_turns("codex", servers, 2) == ["t0", "t1"]
