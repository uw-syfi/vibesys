"""Experiment-chat prompt selection over a reused provider conversation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from agentshim.testing import FakeExecutor, FakeRun, scripted_turn
from tests.server.support import build_server_parts

from server.chat.prompts import (
    experiment_chat_continuation_prompt,
    experiment_chat_system_prompt,
)
from server.chat.session import ExperimentChatDependencies, ExperimentChatSession
from server.events import EventType
from vibesys.render import output_sink
from vs_agent.api import AgentClient, AgentSessionKey, MCPServerSpec, SessionScope
from vs_agent.api.testing import FakeAgentClient
from vs_agent.drivers import agentshim as agentshim_driver

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import agentshim

    from server.chat.session import ChatAgentClient
    from server.controller import RunController
    from server.execution import ExecutionTracker
    from vs_agent.api import AgentClientProtocol
_SHARED_STATE_DIR = "/state/server/chat"
_FULL_PROMPT = experiment_chat_system_prompt(_SHARED_STATE_DIR)
_CONTINUATION_PROMPT = experiment_chat_continuation_prompt(_SHARED_STATE_DIR)
_TOOL_SERVERS = (
    MCPServerSpec(
        name="vibesys-run", command="python", args=("-m", "vibesys.api.chat_tools_server")
    ),
)


def _chat(
    tmp_path: Path,
    client: AgentClientProtocol,
    *,
    thread_id: str | None = None,
    executions: ExecutionTracker | None = None,
    controller: RunController | None = None,
) -> ExperimentChatSession:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    if controller is None:
        mock_controller = MagicMock()
        mock_controller.start_agent_execution.return_value = SimpleNamespace(execution_id="exec-1")
        controller = cast("RunController", mock_controller)
    return ExperimentChatSession(
        ExperimentChatDependencies(
            controller=controller,
            executions=(
                cast("ExecutionTracker", MagicMock()) if executions is None else executions
            ),
            agent_client=cast("ChatAgentClient", client),
            session_key=AgentSessionKey(SessionScope.CHAT, thread_id or "default"),
            chat_thread_id=thread_id,
            workspace=workspace,
            state_dir=tmp_path / "state",
            agent_state_dir=_SHARED_STATE_DIR,
            mcp_servers=_TOOL_SERVERS,
            log=lambda _message: None,
            environment=dict,
            progress=lambda: None,
            driver="agentshim",
            provider="codex",
            model="gpt-test",
            fallback=lambda _question: "fallback",
        )
    )


def test_chat_reuses_a_conversation_keyed_by_thread(tmp_path: Path) -> None:
    fake = FakeAgentClient()
    chat = _chat(tmp_path, fake, thread_id="thread-a")

    chat.ask("what happened?")

    call = fake.calls_for("chat")[0]
    assert call.reuse_session is True
    assert call.session_key == AgentSessionKey(SessionScope.CHAT, "thread-a")


def test_chat_sends_the_full_prompt_when_no_conversation_is_named(tmp_path: Path) -> None:
    fake = FakeAgentClient()
    chat = _chat(tmp_path, fake)

    chat.ask("what happened?")

    assert [call.system_prompt for call in fake.calls_for("chat")] == [_FULL_PROMPT]


def test_chat_passes_the_investigation_tool_servers_to_the_agent(tmp_path: Path) -> None:
    fake = FakeAgentClient()
    chat = _chat(tmp_path, fake)

    chat.ask("what happened?")

    assert fake.calls_for("chat")[0].mcp_servers == list(_TOOL_SERVERS)


def test_chat_shortens_the_prompt_inside_a_named_conversation(tmp_path: Path) -> None:
    key = AgentSessionKey(SessionScope.CHAT, "default")
    fake = FakeAgentClient().set_session(
        key, provider_session_id="session-1", last_turn="session-1"
    )
    chat = _chat(tmp_path, fake)

    chat.ask("what happened?")

    assert [call.system_prompt for call in fake.calls_for("chat")] == [_CONTINUATION_PROMPT]


def test_chat_reasks_when_the_turn_ran_in_a_different_conversation(tmp_path: Path) -> None:
    key = AgentSessionKey(SessionScope.CHAT, "default")
    fake = FakeAgentClient().set_text("chat", "It improved in round 2.")
    # The driver replaced the conversation while serving the turn, so the
    # shortened prompt reached an agent that never saw the read-only rules.
    fake.set_session(key, provider_session_id="session-1", last_turn="session-2")
    chat = _chat(tmp_path, fake)

    answer = chat.ask("what happened?")

    assert [call.system_prompt for call in fake.calls_for("chat")] == [
        _CONTINUATION_PROMPT,
        _FULL_PROMPT,
    ]
    assert answer.text == "It improved in round 2."


def test_chat_keeps_one_answer_when_the_conversation_retires_after_the_turn(
    tmp_path: Path,
) -> None:
    key = AgentSessionKey(SessionScope.CHAT, "default")
    # The turn ran where the shortened prompt assumed; the conversation was
    # retired only afterwards, so its answer stands.
    fake = FakeAgentClient().set_session(
        key, provider_session_id="session-1", last_turn="session-1"
    )
    chat = _chat(tmp_path, fake)
    chat.ask("what happened?")
    fake.evict_session(key)

    chat.ask("and then?")

    assert [call.system_prompt for call in fake.calls_for("chat")] == [
        _CONTINUATION_PROMPT,
        _FULL_PROMPT,
    ]


def test_chat_answer_carries_the_invocation_that_produced_it(tmp_path: Path) -> None:
    fake = FakeAgentClient()
    chat = _chat(tmp_path, fake)

    answer = chat.ask("what happened?")

    # The answer's identity is the same id the turn's streamed chunks carried
    # (the presentation scope's invocation_id), so clients can fold the
    # terminal answer over exactly that turn.
    assert answer.invocation_id == "exec-1"
    assert fake.calls_for("chat")[0].invocation_id == "exec-1"


def test_chat_reask_stamps_the_answer_with_the_second_invocation(tmp_path: Path) -> None:
    key = AgentSessionKey(SessionScope.CHAT, "default")
    fake = FakeAgentClient().set_session(
        key, provider_session_id="session-1", last_turn="session-2"
    )
    controller = MagicMock()
    controller.start_agent_execution.side_effect = [
        SimpleNamespace(execution_id="exec-1"),
        SimpleNamespace(execution_id="exec-2"),
    ]
    chat = _chat(tmp_path, fake, controller=controller)

    answer = chat.ask("what happened?")

    # The re-ask is a second invocation; the returned answer came from it, so
    # its identity (not the abandoned first invocation's) is stamped.
    assert len(fake.calls_for("chat")) == 2
    assert answer.invocation_id == "exec-2"


def test_chat_empty_answer_fallback_keeps_the_turn_identity(tmp_path: Path) -> None:
    fake = FakeAgentClient().set_text("chat", "   ")
    chat = _chat(tmp_path, fake)

    answer = chat.ask("what happened?")

    # The turn still ran under exec-1, so the substitute text closes that
    # streamed turn rather than dangling as an unowned answer.
    assert answer.text.startswith("Chat agent did not return an answer.")
    assert answer.invocation_id == "exec-1"


def test_chat_never_reasks_a_cold_turn(tmp_path: Path) -> None:
    key = AgentSessionKey(SessionScope.CHAT, "default")
    fake = FakeAgentClient().set_session(key, last_turn="session-1")
    chat = _chat(tmp_path, fake)

    chat.ask("what happened?")

    # Nothing justified a shortened prompt, so a new conversation is expected.
    assert len(fake.calls_for("chat")) == 1


def _chat_driver(
    provider: str,
    runs: Callable[[agentshim.CommandRequest], FakeRun],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AgentClient, FakeExecutor]:
    """Drive the real driver and client over a scripted provider process."""
    monkeypatch.setattr(agentshim_driver, "build_host_sandbox", lambda *_a, **_k: None)
    fake = FakeExecutor(runs)
    driver = agentshim_driver.AgentShimDriver(
        provider=provider,
        executor_factory=lambda: fake,
    )
    return AgentClient(driver, provider=provider), fake


def _prompts(fake: FakeExecutor) -> list[str]:
    """Every prompt the provider was given, in the order the turns ran."""
    return [request.stdin or "" for request in fake.requests]


def test_codex_thread_renewal_puts_the_read_only_rules_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Codex thread budget retires a thread after a fixed number of turns.
    # Before drivers reported that, chat kept shortening its prompt into the
    # replacement thread, so a question periodically ran with no read-only
    # rules at all. The driver now reports the restart, the client drops the
    # conversation, and the next question carries the full instructions again.
    client, fake = _chat_driver(
        "codex",
        lambda _request: scripted_turn("codex", text="done", session_id="thread-1"),
        monkeypatch,
    )
    chat = _chat(tmp_path, client)

    for question in ("what happened?", "and then?", "why?"):
        chat.ask(question)

    prompts = _prompts(fake)
    assert len(prompts) == 3, "one provider turn per question, with no re-asks"
    assert prompts[0].startswith(_FULL_PROMPT)
    assert prompts[1].startswith(_CONTINUATION_PROMPT)
    assert prompts[2].startswith(_FULL_PROMPT)


def test_stale_claude_session_reasks_instead_of_failing_the_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusals = [1]
    conversations: list[str] = []

    def run(request: agentshim.CommandRequest) -> FakeRun:
        live = conversations[-1] if conversations else None
        if live is not None and live in request.argv:
            if refusals:
                # Claude Code reports a refused resume as a bare nonzero exit.
                refusals.pop()
                return FakeRun(returncode=1, stderr=["claude exited with code 1\n"])
            return scripted_turn("claude", text="done", session_id=live)
        conversations.append(f"session-{len(conversations) + 1}")
        return scripted_turn("claude", text="done", session_id=conversations[-1])

    client, fake = _chat_driver("claude", run, monkeypatch)
    chat = _chat(tmp_path, client)
    chat.ask("what happened?")

    answer = chat.ask("and then?")

    prompts = _prompts(fake)
    # The shortened prompt, the driver's own retry of it from a fresh
    # conversation, then the question asked again with the rules restored.
    assert prompts[1].startswith(_CONTINUATION_PROMPT)
    assert prompts[2].startswith(_CONTINUATION_PROMPT)
    assert prompts[3].startswith(_FULL_PROMPT)
    assert answer.text == "done"


@pytest.mark.parametrize("thread_id", [None, "thread-a"])
def test_streamed_output_is_filed_under_the_thread_that_asked(
    tmp_path: Path, thread_id: str | None
) -> None:
    parts = build_server_parts(tmp_path)

    # ``output_sink()`` is the same event sink `build_chat_agent` injects into
    # a real client's construction; `parts.integration` (built by
    # `build_server_parts`) is already subscribed to it, so a chunk emitted
    # here reaches `parts.journal` exactly as a real streaming driver's would.
    fake = FakeAgentClient(event_sink=output_sink())
    fake.stream_output("chat", ["partial "])

    chat = _chat(
        tmp_path,
        fake,
        thread_id=thread_id,
        executions=parts.executions,
        controller=parts.controller,
    )

    chat.ask("what happened?")

    assert [
        (event.agent_kind, event.chat_thread_id)
        for event in parts.journal.read()
        if event.type is EventType.AGENT_OUTPUT_CHUNK
    ] == [("chat", thread_id)]


def test_chat_normalizes_an_agent_failure(tmp_path: Path) -> None:
    fake = FakeAgentClient().fail("chat", ValueError("no such workspace"))
    chat = _chat(tmp_path, fake)

    with pytest.raises(RuntimeError, match="Chat agent failed: ValueError: no such workspace"):
        chat.ask("what happened?")

    assert len(fake.calls_for("chat")) == 1


def test_chat_propagates_a_cancellation_unwrapped(tmp_path: Path) -> None:
    fake = FakeAgentClient().fail("chat", KeyboardInterrupt())
    chat = _chat(tmp_path, fake)

    # A cancellation is not an agent error and must not be reported as one.
    with pytest.raises(KeyboardInterrupt):
        chat.ask("what happened?")
