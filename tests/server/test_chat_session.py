"""Experiment-chat behavior over the managed auxiliary-agent contract."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from tests.server.support import ServerParts, build_server_parts

from server.chat.session import ExperimentChatDependencies, ExperimentChatSession
from server.events import EventType
from vibesys.run import CoreAgentEventSink

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclass
class _FakeManagedAgent:
    """Deterministic in-memory implementation of ``ManagedAgent``."""

    answers: deque[str] = field(default_factory=lambda: deque(["answer"]))
    failure: BaseException | None = None
    on_turn: Callable[[], None] | None = None
    messages: list[str] = field(default_factory=list)
    invocation_ids: list[str | None] = field(default_factory=list)
    closed: bool = False

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        if self.closed:
            error = "fake managed agent is closed"
            raise RuntimeError(error)
        self.messages.append(message)
        self.invocation_ids.append(invocation_id)
        if self.failure is not None:
            raise self.failure
        if self.on_turn is not None:
            self.on_turn()
        return self.answers.popleft() if self.answers else "answer"

    def close(self) -> None:
        self.closed = True


def _chat(
    tmp_path: Path,
    agent: _FakeManagedAgent,
    *,
    thread_id: str | None = None,
) -> tuple[ExperimentChatSession, ServerParts]:
    parts = build_server_parts(tmp_path)
    chat = ExperimentChatSession(
        ExperimentChatDependencies(
            controller=parts.controller,
            executions=parts.executions,
            agent=agent,
            chat_thread_id=thread_id,
            state_dir=tmp_path / "state",
            driver="agentshim",
            provider="codex",
            model="gpt-test",
            fallback=lambda _question: "fallback",
        ),
        agent,
    )
    return chat, parts


def test_chat_turn_carries_the_server_invocation_and_persists_exchange(tmp_path: Path) -> None:
    agent = _FakeManagedAgent(deque(["It improved in round 2."]))
    chat, parts = _chat(tmp_path, agent)

    answer = chat.ask("what happened?")

    assert answer.text == "It improved in round 2."
    assert answer.invocation_id
    assert agent.messages == ["what happened?"]
    assert agent.invocation_ids == [answer.invocation_id]
    assert (tmp_path / "state" / "conversation.jsonl").read_text(encoding="utf-8") == (
        '{"question": "what happened?", "answer": "It improved in round 2."}\n'
    )
    assert not parts.executions.active_locked()


def test_chat_empty_answer_uses_fallback_without_changing_invocation(tmp_path: Path) -> None:
    chat, _parts = _chat(tmp_path, _FakeManagedAgent(deque(["   "])))

    answer = chat.ask("what happened?")

    assert answer.text.startswith("Chat agent did not return an answer.")
    assert "fallback" in answer.text
    assert answer.invocation_id


@pytest.mark.parametrize("thread_id", [None, "thread-a"])
def test_streamed_output_is_filed_under_the_thread_that_asked(
    tmp_path: Path, thread_id: str | None
) -> None:
    parts = build_server_parts(tmp_path)
    events = CoreAgentEventSink(parts.integration.project_event)
    agent = _FakeManagedAgent(on_turn=lambda: events.agent_output("partial ", agent_kind="chat"))
    chat = ExperimentChatSession(
        ExperimentChatDependencies(
            controller=parts.controller,
            executions=parts.executions,
            agent=agent,
            chat_thread_id=thread_id,
            state_dir=tmp_path / "state",
            driver="agentshim",
            provider="codex",
            model="gpt-test",
            fallback=lambda _question: "fallback",
        ),
        agent,
    )

    chat.ask("what happened?")

    assert [
        (event.agent_kind, event.chat_thread_id)
        for event in parts.journal.read()
        if event.type is EventType.AGENT_OUTPUT_CHUNK
    ] == [("chat", thread_id)]


def test_chat_normalizes_an_agent_failure(tmp_path: Path) -> None:
    agent = _FakeManagedAgent(failure=ValueError("no such workspace"))
    chat, _parts = _chat(tmp_path, agent)

    with pytest.raises(RuntimeError, match="Chat agent failed: ValueError: no such workspace"):
        chat.ask("what happened?")

    assert agent.messages == ["what happened?"]


def test_chat_propagates_cancellation_and_closes_agent_once(tmp_path: Path) -> None:
    agent = _FakeManagedAgent(failure=KeyboardInterrupt())
    chat, _parts = _chat(tmp_path, agent)

    with pytest.raises(KeyboardInterrupt):
        chat.ask("what happened?")
    chat.close()
    chat.close()

    assert agent.closed
