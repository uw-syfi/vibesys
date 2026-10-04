"""Durable continuations use ordinary turn events and usage accounting."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vibesys.api import (
    AgentOutputChunkData,
    CoreAgentEventSink,
    CoreEventType,
    ToolCallData,
    ToolResultData,
)
from vs_agent.api import (
    AgentClient,
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentUsage,
    ClientAgentSessions,
    Completed,
    SessionScope,
    Unknown,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeDriver
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import CoreEvent


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("tokens", [None, 53])
def test_resume_publishes_turn_events_and_one_usage_record(
    tmp_path: Path, *, streamed: bool, tokens: int | None
) -> None:
    events: list[CoreEvent] = []
    turn_events = [
        AgentEvent(AgentEventKind.TOOL_CALL, payload={"tool": "Bash", "args": {"command": "ls"}}),
        AgentEvent(
            AgentEventKind.TOOL_RESULT, payload={"tool": "Bash", "stdout": "files", "exit_code": 0}
        ),
        AgentEvent(
            AgentEventKind.USAGE, usage=AgentUsage(input_tokens=tokens, output_tokens=tokens)
        ),
    ]
    if streamed:
        turn_events.append(AgentEvent(AgentEventKind.TEXT, text="continued answer"))
    driver = FakeDriver(turns=[[], turn_events], answer="continued answer")
    key = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=tmp_path,
        model="test-model",
        reasoning_effort="high",
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    (tmp_path / "resume.j2").write_text("Evaluation settled.", encoding="utf-8")
    message = TemplateRenderer(tmp_path).render_template("resume.j2")
    with AgentClient(
        driver,
        provider="codex",
        log_dir=tmp_path,
        model_name="test-model",
        default_reasoning_effort="high",
        event_sink=CoreAgentEventSink(events.append),
    ) as client:
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="test",
            user_prompt="first",
            round_label="round 1",
            session_key=key,
        )
        events.clear()
        sessions = ClientAgentSessions(client, FakeAgentInvocationStore())
        sessions.bind(key, spec, AgentTurnRequest(message="", label="round 2"))
        outcome = sessions.resume(key, message, "evaluation-resume-1")
        assert isinstance(outcome, Completed)
        assert sessions.resume(key, message, "evaluation-resume-1") == outcome
        assert sessions.inspect(key, "evaluation-resume-1") == outcome
    records = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert records[1]["kind"] == "implementer"
    assert records[1]["round_label"] == "round 2"
    assert records[1]["model"] == "test-model"
    assert records[1]["reasoning_effort"] == "high"
    assert records[1]["input_tokens"] == tokens
    assert records[1]["output_tokens"] == tokens
    calls = [event for event in events if event.type is CoreEventType.TOOL_CALL]
    assert len(calls) == 1
    assert isinstance(calls[0].data, ToolCallData)
    assert calls[0].data.tool == "Bash"
    assert calls[0].execution_id == "evaluation-resume-1"
    assert calls[0].agent_kind == "implementer"
    assert calls[0].round_label == "round 2"
    results = [event for event in events if event.type is CoreEventType.TOOL_RESULT]
    assert len(results) == 1
    assert isinstance(results[0].data, ToolResultData)
    assert results[0].data.content == "files"
    assert results[0].data.call_id == calls[0].data.call_id
    assert results[0].execution_id == "evaluation-resume-1"
    chunks = [
        (event, event.data)
        for event in events
        if event.type is CoreEventType.AGENT_OUTPUT_CHUNK
        and isinstance(event.data, AgentOutputChunkData)
        and event.data.channel == "assistant"
    ]
    assert "".join(chunk.content for _, chunk in chunks).strip() == "continued answer"
    assert all(event.execution_id == "evaluation-resume-1" for event, _ in chunks)


def test_failed_resume_writes_unknown_usage_once_and_never_replays(tmp_path: Path) -> None:
    attempts = 0

    def fail_resumed_turn(_request: AgentTurnRequest) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            detail = "lost provider acknowledgement"
            raise LookupError(detail)

    driver = FakeDriver(answer="first answer", on_turn=fail_resumed_turn)
    key = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")
    spec = AgentSessionSpec(
        role="implementer",
        provider="codex",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    (tmp_path / "resume.j2").write_text("Evaluation settled.", encoding="utf-8")
    message = TemplateRenderer(tmp_path).render_template("resume.j2")
    with AgentClient(driver, provider="codex", log_dir=tmp_path) as client:
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="test",
            user_prompt="first",
            round_label="round 1",
            session_key=key,
        )
        sessions = ClientAgentSessions(client, FakeAgentInvocationStore())
        sessions.bind(key, spec, AgentTurnRequest(message="", label="round 2"))
        outcome = sessions.resume(key, message, "evaluation-resume-1")
        assert isinstance(outcome, Unknown)
        assert sessions.resume(key, message, "evaluation-resume-1") == outcome
    records = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert records[1]["input_tokens"] is None
    assert records[1]["output_tokens"] is None
    assert attempts == 2
