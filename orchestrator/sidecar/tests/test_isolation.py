"""A launched Claude agent sees only the orchestrator's MCP server and none of the operator's setup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentshim.testing import ClaudeFramePeer, FakeExecutor

from tests import helpers as h
from vs_sidecar.driver import AgentshimDriver, build_session
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshim import RawFrame

SESSION = "11111111-1111-4111-8111-111111111111"
INIT: dict[str, Any] = {"type": "system", "subtype": "init", "session_id": SESSION, "model": "m"}
SERVER = sc.ToolServer(name="vibesys", argv=["/bin/vibesys-host", "mcp"])


def _claude_argv() -> list[str]:
    executor = FakeExecutor([], peers=lambda _request: ClaudeFramePeer([[INIT]]))
    agent = h.scripted_agents("claude", executor)
    request = h.open_request(orch.HARNESS_CLAUDE_CODE, tools=[SERVER])

    def make(raw_sink: Callable[[RawFrame], None]) -> AgentshimDriver:
        return AgentshimDriver(
            lambda conversation_id: build_session(
                request,
                agent=agent,
                raw_sink=raw_sink,
                new_conversation_id=conversation_id,
            ),
            preferred_id=SESSION,
        )

    engine = Engine(request.session, MemoryStore(), make)
    engine.start()
    engine.release()
    assert executor.spawns, "the session never started its agent"
    return executor.spawns[0].inlined_argv()


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


def test_claude_cannot_schedule_a_turn_of_its_own_because_only_the_host_starts_turns() -> None:
    argv = _claude_argv()
    assert argv.count("--disallowedTools") == 1
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"ScheduleWakeup", "CronCreate"} <= set(denied)
