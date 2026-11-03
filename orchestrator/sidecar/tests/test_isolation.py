"""A launched Claude agent sees only the orchestrator's MCP server and none of the operator's setup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentshim import (
    StdioMcpServer,
)
from agentshim.testing import ClaudeFramePeer, FakeExecutor

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshim import RawFrame

SESSION = "11111111-1111-4111-8111-111111111111"
INIT: dict[str, Any] = {"type": "system", "subtype": "init", "session_id": SESSION, "model": "m"}
SERVER = StdioMcpServer(name="vibesys", command="/bin/vibesysd", args=("mcp",), env={})


def _claude_argv() -> list[str]:
    executor = FakeExecutor([], peers=lambda _request: ClaudeFramePeer([[INIT]]))
    agent = h.scripted_agents("claude", executor)
    launch = h.launch(orch.HARNESS_CLAUDE_CODE)

    def make(raw_sink: Callable[[RawFrame], None]) -> AgentshimDriver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                launch,
                agent=agent,
                mcp_servers=[SERVER],
                raw_sink=raw_sink,
                new_conversation_id=conversation_id,
            ),
            preferred_id=SESSION,
        )

    engine = Engine(launch, MemoryStore(), make)
    engine.start()
    engine.release()
    assert executor.spawns, "the session never started its agent"
    return list(executor.spawns[0].argv)


def test_claude_gets_only_the_given_mcp_servers_and_none_of_the_operators_settings() -> None:
    argv = _claude_argv()
    assert "--strict-mcp-config" in argv
    config = argv[argv.index("--mcp-config") + 1]
    assert '"vibesys"' in config
    sources = argv[argv.index("--setting-sources") + 1]
    assert "user" not in sources.split(",")


def test_claude_cannot_delegate_around_the_orchestrator_or_message_other_sessions() -> None:
    argv = _claude_argv()
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"Agent", "Task", "SendMessage", "ListAgents"} <= set(denied)
