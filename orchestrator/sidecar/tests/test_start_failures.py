"""A session opens only once its agent is up with its tools, and every way it cannot ends it at once.

Each test starts a real ``Engine`` over the real ``AgentshimDriver`` and agentshim's scripted
providers, injects one fault at one stage of the start, and holds the log to the single outcome:
one ``failed`` event with the reason, nothing else (the session was never reported open), and no
agent process left running.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from agentshim import CliNotFoundError, FailureKind, StdioMcpServer, TurnFailedError
from agentshim.testing import (
    ClaudeRecordedPeer,
    ClaudeStreamPeers,
    CodexScript,
    FakeExecutor,
    FakeRun,
)

from tests import helpers as h
from vs_sidecar.driver import StartError, classify_start_error
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc
from vs_sidecar.store import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentshim.execution.process import SpawnRequest

TOOLS = (StdioMcpServer("vibesys", "/bin/vibesys", ("mcp",)),)


class _NoDirectory(FakeExecutor):
    """A host where the agent's working directory cannot be entered, as ``spawn`` finds out."""

    def spawn(self, request: SpawnRequest) -> Any:
        raise FileNotFoundError(2, "No such file or directory", request.cwd)


def _start(
    provider: str, executor: FakeExecutor, mcp_servers: Sequence[StdioMcpServer] = TOOLS
) -> list[sc.SidecarEvent]:
    factory = h.driver_factory_over(provider, executor, mcp_servers=mcp_servers)
    engine = Engine(h.launch(harness=_harness(provider)), MemoryStore(), factory)
    engine.start()
    return engine.events_after(0)


def _harness(provider: str) -> int:
    return orch.HARNESS_CLAUDE_CODE if provider == "claude" else orch.HARNESS_CODEX


def _claude(peers: ClaudeStreamPeers, **executor: Any) -> FakeExecutor:
    return FakeExecutor([], peers=peers.build, **executor)


def _fails_with(events: Sequence[sc.SidecarEvent], reason: int, detail: str) -> None:
    assert h.kinds(events) == ["failed"], "the session must never be reported open"
    failed = events[0].failed
    assert failed.start_failure_reason == reason
    assert failed.detail.startswith("could not start the agent: ")
    assert detail in failed.detail
    assert "sidecar" not in failed.detail


def _no_agent_left(executor: FakeExecutor) -> None:
    for process in executor.processes:
        ended = process.stdin_closed or process.terminate_calls or process.kill_calls
        assert ended, "an agent process was left running"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_missing_agent_program_fails_the_start_naming_it(provider: str) -> None:
    executor = FakeExecutor([], binaries={"unrelated": "/bin/unrelated"})
    events = _start(provider, executor)
    _fails_with(
        events, orch.START_FAILURE_REASON_AGENT_NOT_INSTALLED, f"{provider} binary not found"
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_working_directory_that_cannot_be_entered_fails_the_start(provider: str) -> None:
    events = _start(provider, _NoDirectory([]))
    _fails_with(events, orch.START_FAILURE_REASON_WORKING_DIRECTORY_UNAVAILABLE, "/work")


def test_a_plugin_that_cannot_be_installed_fails_the_start_and_starts_no_agent() -> None:
    executor = FakeExecutor(FakeRun(returncode=1, stderr=["marketplace unreachable"]))
    events = _start("claude", executor)
    _fails_with(events, orch.START_FAILURE_REASON_PLUGIN_UNAVAILABLE, "marketplace unreachable")
    assert executor.spawns == []


def test_a_sign_in_the_agent_refuses_at_start_fails_the_start() -> None:
    refusal = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 401,
        "result": "Invalid API key · Please run /login",
    }
    peer = ClaudeRecordedPeer(json.dumps(refusal) + "\n")
    executor = FakeExecutor([], peers=lambda _request: peer)
    events = _start("claude", executor)
    _fails_with(events, orch.START_FAILURE_REASON_NOT_SIGNED_IN, "Please run /login")


def test_a_tool_server_claude_reports_failed_fails_the_start_and_ends_the_agent() -> None:
    executor = _claude(ClaudeStreamPeers(mcp_status=[{"vibesys": ("failed", "spawn ENOENT")}]))
    events = _start("claude", executor)
    _fails_with(
        events,
        orch.START_FAILURE_REASON_TOOLS_UNAVAILABLE,
        "tool server vibesys failed: spawn ENOENT",
    )
    _no_agent_left(executor)


def test_a_tool_server_codex_reports_failed_fails_the_start_and_ends_the_agent() -> None:
    script = CodexScript()
    script.fail_mcp_server("vibesys", "No such file or directory")
    executor = FakeExecutor([], peers=script.peer)
    events = _start("codex", executor)
    _fails_with(events, orch.START_FAILURE_REASON_TOOLS_UNAVAILABLE, "No such file or directory")
    _no_agent_left(executor)


# What a provider cannot say is unknown, and unknown opens the session: no run fails on missing
# evidence.
@pytest.mark.parametrize(
    "answers",
    [
        [{"vibesys": ("connected", "")}],
        [{"vibesys": ("pending", "")}],
        [{"vibesys": ("hibernating", "")}],
        [{}],
        None,
    ],
    ids=["connected", "still pending", "a state nobody knows", "not listed", "question refused"],
)
def test_tools_that_are_connected_or_unknown_open_the_session(answers: Any) -> None:
    executor = _claude(ClaudeStreamPeers(mcp_status=answers))
    events = _start("claude", executor)
    assert h.kinds(events) == ["opened"]


def test_codex_servers_that_started_open_the_session() -> None:
    events = _start("codex", FakeExecutor([], peers=CodexScript().peer))
    assert h.kinds(events) == ["opened"]


def test_a_session_given_no_tool_servers_opens() -> None:
    events = _start("claude", _claude(ClaudeStreamPeers()), mcp_servers=())
    assert h.kinds(events) == ["opened"]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CliNotFoundError("claude"), orch.START_FAILURE_REASON_AGENT_NOT_INSTALLED),
        (
            TurnFailedError("401", kind=FailureKind.AUTH),
            orch.START_FAILURE_REASON_NOT_SIGNED_IN,
        ),
        (
            TurnFailedError("overloaded", kind=FailureKind.TRANSIENT),
            orch.START_FAILURE_REASON_OTHER,
        ),
        (
            FileNotFoundError(2, "gone", "/work"),
            orch.START_FAILURE_REASON_WORKING_DIRECTORY_UNAVAILABLE,
        ),
        (FileNotFoundError(2, "gone", "/elsewhere"), orch.START_FAILURE_REASON_OTHER),
        (OSError("no filename"), orch.START_FAILURE_REASON_OTHER),
        (ValueError("plugin installation failed: x"), orch.START_FAILURE_REASON_OTHER),
    ],
)
def test_an_error_is_classified_by_its_type_and_never_by_its_text(
    error: Exception, reason: int
) -> None:
    assert classify_start_error(error, "/work") == reason


def test_a_start_failure_names_its_reason_in_the_driver_error() -> None:
    factory = h.driver_factory_over(
        "claude", FakeExecutor([], binaries={"x": "/x"}), mcp_servers=TOOLS
    )
    driver = factory(lambda _frame: None)
    with pytest.raises(StartError) as raised:
        driver.open()
    assert raised.value.reason == orch.START_FAILURE_REASON_AGENT_NOT_INSTALLED


def test_every_stage_that_fails_to_start_leaves_one_event_and_no_process() -> None:
    stages: list[tuple[str, Callable[[], tuple[FakeExecutor, str]]]] = [
        (
            "tools",
            lambda: (
                _claude(ClaudeStreamPeers(mcp_status=[{"vibesys": ("failed", "x")}])),
                "claude",
            ),
        ),
        ("program", lambda: (FakeExecutor([], binaries={"y": "/y"}), "claude")),
        ("directory", lambda: (_NoDirectory([]), "codex")),
    ]
    for name, build in stages:
        executor, provider = build()
        events = _start(provider, executor)
        assert h.kinds(events) == ["failed"], name
        assert events[0].failed.start_failure_reason != orch.START_FAILURE_REASON_UNSPECIFIED, name
        _no_agent_left(executor)
