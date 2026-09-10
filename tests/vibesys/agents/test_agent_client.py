from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from vibesys.agents.client import AgentClient
from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentObserver,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
    SessionDisposition,
)
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.render.sink import output_sink
from vibesys.run.events import (
    AgentOutputChunkData,
    CommandResultPayload,
    CoreEventType,
    ToolResultData,
)


class _Response(BaseModel):
    answer: str


@dataclass
class _FakeSession:
    results: list[AgentTurnResult]
    turns: list[AgentTurnRequest] = field(default_factory=list)
    close_calls: int = 0
    error: BaseException | None = None
    observers: list[AgentObserver | None] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    lifecycle_calls: list[str] = field(default_factory=list)
    cancel_error: BaseException | None = None

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        self.turns.append(request)
        self.observers.append(observer)
        if observer is not None:
            for event in self.events:
                observer.on_event(event)
        if self.error is not None:
            raise self.error
        return self.results.pop(0)

    def resume_provider_session(self, session_id: str) -> bool:
        """Adopt ``session_id``, recording it so tests can assert on it."""
        self.resumed.append(session_id)
        return True

    def cancel(self) -> None:
        self.lifecycle_calls.append("cancel")
        if self.cancel_error is not None:
            raise self.cancel_error

    def close(self) -> None:
        self.lifecycle_calls.append("close")
        self.close_calls += 1


@dataclass
class _FakeDriver:
    queued_sessions: list[_FakeSession]
    specs: list[AgentSessionSpec] = field(default_factory=list)
    close_calls: int = 0

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities()

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        self.specs.append(spec)
        return self.queued_sessions.pop(0)

    def close(self) -> None:
        self.close_calls += 1


def _key(identifier: str) -> AgentSessionKey:
    return AgentSessionKey(SessionScope.HYPOTHESIS, identifier)


def _spec(
    *,
    model: str = "model",
    workspace: Path = Path("/workspace"),
    skills: tuple[Path, ...] = (),
) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider="codex",
        model=model,
        workspace=workspace,
        policy=AgentExecutionPolicy(),
        skills=skills,
    )


def test_keyed_turns_reuse_a_session() -> None:
    session = _FakeSession(
        results=[AgentTurnResult("first"), AgentTurnResult("second")],
    )
    driver = _FakeDriver([session])
    client = AgentClient(driver)

    assert (
        client.run(
            session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=_key("impl")
        ).text
        == "first"
    )
    assert (
        client.run(
            session_spec=_spec(), turn=AgentTurnRequest("two"), session_key=_key("impl")
        ).text
        == "second"
    )

    assert len(driver.specs) == 1
    assert session.turns == [AgentTurnRequest("one"), AgentTurnRequest("two")]


def test_session_setup_materializes_skills_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(results=[AgentTurnResult("first"), AgentTurnResult("second")])
    client = AgentClient(_FakeDriver([session]))
    skill = tmp_path / "source-skill"
    calls: list[tuple[Path, list[Path]]] = []
    monkeypatch.setattr(
        "vibesys.agents.client.materialize_skills",
        lambda workspace, skills, **_kwargs: calls.append((workspace, skills)),
    )
    spec = _spec(workspace=tmp_path, skills=(skill,))

    client.run(session_spec=spec, turn=AgentTurnRequest("one"), session_key=_key("impl"))
    client.run(session_spec=spec, turn=AgentTurnRequest("two"), session_key=_key("impl"))

    assert calls == [(tmp_path, [skill])]


def test_client_forwards_observer_to_session() -> None:
    session = _FakeSession(results=[AgentTurnResult("done")])
    client = AgentClient(_FakeDriver([session]))

    @dataclass
    class Observer:
        events: list[AgentEvent] = field(default_factory=list)

        def on_event(self, event: AgentEvent) -> None:
            self.events.append(event)

    observer = Observer()
    client.run(
        session_spec=_spec(),
        turn=AgentTurnRequest("one"),
        observer=observer,
    )

    assert session.observers == [observer]
    observer.on_event(AgentEvent(AgentEventKind.TEXT, text="chunk"))
    assert observer.events == [AgentEvent(AgentEventKind.TEXT, text="chunk")]


def test_invoke_preserves_streamed_text_deltas_and_paragraphs(tmp_path: Path) -> None:
    deltas = ["Tokens", " stay", " together.\n\n", "Next paragraph."]
    session = _FakeSession(
        results=[AgentTurnResult("".join(deltas))],
        events=[AgentEvent(AgentEventKind.TEXT, text=delta) for delta in deltas],
    )
    log = io.StringIO()
    client = AgentClient(_FakeDriver([session]), run_log_file=log)
    seen = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="user",
            round_label="round-1",
        )
    finally:
        unsubscribe()

    assistant_chunks = [
        event.data.content
        for event in seen
        if event.type is CoreEventType.AGENT_OUTPUT_CHUNK
        and isinstance(event.data, AgentOutputChunkData)
        and event.data.channel == "assistant"
        and event.agent_kind == "implementer"
    ]
    assert assistant_chunks == [*deltas, "\n"]
    assert "Tokens stay together.\n\nNext paragraph.\n" in log.getvalue()


def test_invoke_threads_typed_tool_result_payload_to_sink(tmp_path: Path) -> None:
    payload = CommandResultPayload(stdout="ok", stderr="warn", exit_code=1, duration=0.4)
    session = _FakeSession(
        results=[AgentTurnResult("Done")],
        events=[
            AgentEvent(
                AgentEventKind.TOOL_RESULT,
                payload={
                    "tool": "shell",
                    "stdout": "ok",
                    "stderr": "warn",
                    "exit_code": 1,
                    "duration": 0.4,
                    "result_payload": payload,
                },
            ),
            AgentEvent(AgentEventKind.TOOL_RESULT, payload={"tool": "shim", "stdout": "plain"}),
        ],
    )
    client = AgentClient(_FakeDriver([session]))
    seen = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="user",
            round_label="round-1",
        )
    finally:
        unsubscribe()

    results = [
        event.data
        for event in seen
        if event.type is CoreEventType.TOOL_RESULT and isinstance(event.data, ToolResultData)
    ]
    assert [data.tool for data in results] == ["shell", "shim"]
    assert results[0].payload == payload
    assert results[0].content == "ok"
    # A driver event without typed structure falls back to the classifier,
    # which leaves plain text unclassified.
    assert results[1].payload is None


def test_invoke_closes_text_before_tool_event(tmp_path: Path) -> None:
    session = _FakeSession(
        results=[AgentTurnResult("Done")],
        events=[
            AgentEvent(AgentEventKind.TEXT, text="Checking"),
            AgentEvent(AgentEventKind.TEXT, text=" now"),
            AgentEvent(AgentEventKind.TOOL_CALL, payload={"tool": "shell", "args": {}}),
            AgentEvent(AgentEventKind.TEXT, text="Done"),
        ],
    )
    client = AgentClient(_FakeDriver([session]))
    seen = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="user",
            round_label="round-1",
        )
    finally:
        unsubscribe()

    relevant = [
        (event.type, getattr(event.data, "content", None))
        for event in seen
        if event.type in (CoreEventType.AGENT_OUTPUT_CHUNK, CoreEventType.TOOL_CALL)
        and event.agent_kind == "implementer"
    ]
    assert relevant == [
        (CoreEventType.AGENT_OUTPUT_CHUNK, "Checking"),
        (CoreEventType.AGENT_OUTPUT_CHUNK, " now"),
        (CoreEventType.AGENT_OUTPUT_CHUNK, "\n"),
        (CoreEventType.TOOL_CALL, None),
        (CoreEventType.AGENT_OUTPUT_CHUNK, "Done"),
        (CoreEventType.AGENT_OUTPUT_CHUNK, "\n"),
    ]


def test_thinking_payload_channel_routes_to_the_diagnostic_channel(tmp_path: Path) -> None:
    session = _FakeSession(
        results=[AgentTurnResult("Done")],
        events=[
            AgentEvent(AgentEventKind.THINKING, text="the hot path is the ring buffer"),
            AgentEvent(
                AgentEventKind.THINKING,
                text="restarting the provider process",
                payload={"channel": "diagnostic"},
            ),
        ],
    )
    client = AgentClient(_FakeDriver([session]))
    seen = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="user",
            round_label="round-1",
        )
    finally:
        unsubscribe()

    # The marked event is plumbing regardless of its wording; the unmarked one
    # is the agent's own reasoning.
    assert [
        (event.data.channel, event.data.content)
        for event in seen
        if isinstance(event.data, AgentOutputChunkData) and event.agent_kind == "implementer"
    ] == [
        ("analysis", "the hot path is the ring buffer"),
        ("diagnostic", "restarting the provider process"),
    ]


def test_invoke_closes_streamed_text_before_reporting_error(tmp_path: Path) -> None:
    session = _FakeSession(
        results=[],
        error=ValueError("failed"),
        events=[AgentEvent(AgentEventKind.TEXT, text="partial output")],
    )
    log = io.StringIO()
    client = AgentClient(_FakeDriver([session]), run_log_file=log)

    with pytest.raises(ValueError, match="failed"):
        client.invoke_text(
            kind="implementer",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="user",
            round_label="round-1",
        )

    assert "partial output\n\n=== Implementer ROUND ERROR" in log.getvalue()


def test_invoke_preserves_terminal_newline_on_cancellation(tmp_path: Path) -> None:
    session = _FakeSession(
        results=[],
        error=KeyboardInterrupt(),
        events=[AgentEvent(AgentEventKind.TEXT, text="partial output\n")],
    )
    client = AgentClient(_FakeDriver([session]))
    seen = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        with pytest.raises(KeyboardInterrupt):
            client.invoke_text(
                kind="implementer",
                workspace=tmp_path,
                system_prompt="system",
                user_prompt="user",
                round_label="round-1",
            )
    finally:
        unsubscribe()

    assistant_chunks = [
        event.data.content
        for event in seen
        if event.type is CoreEventType.AGENT_OUTPUT_CHUNK
        and isinstance(event.data, AgentOutputChunkData)
        and event.data.channel == "assistant"
        and event.agent_kind == "implementer"
    ]
    assert assistant_chunks == ["partial output\n"]


def test_changed_session_spec_closes_and_replaces_cached_session() -> None:
    old = _FakeSession(results=[AgentTurnResult("old")])
    new = _FakeSession(results=[AgentTurnResult("new")])
    driver = _FakeDriver([old, new])
    client = AgentClient(driver)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=_key("impl"))
    result = client.run(
        session_spec=_spec(model="changed"),
        turn=AgentTurnRequest("two"),
        session_key=_key("impl"),
    )

    assert result.text == "new"
    assert old.close_calls == 1
    assert len(driver.specs) == 2


def test_unkeyed_turn_always_closes_ephemeral_session() -> None:
    session = _FakeSession(results=[AgentTurnResult("done")])
    client = AgentClient(_FakeDriver([session]))

    result = client.run(session_spec=_spec(), turn=AgentTurnRequest("one"))

    assert result.text == "done"
    assert session.close_calls == 1


@pytest.mark.parametrize(
    "result",
    [AgentTurnResult("reset", disposition=SessionDisposition.RESET_REQUIRED)],
)
def test_reset_disposition_evicts_session(result: AgentTurnResult) -> None:
    first = _FakeSession(results=[result])
    second = _FakeSession(results=[AgentTurnResult("recovered")])
    driver = _FakeDriver([first, second])
    client = AgentClient(driver)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=_key("impl"))
    client.run(session_spec=_spec(), turn=AgentTurnRequest("two"), session_key=_key("impl"))

    assert first.close_calls == 1
    assert len(driver.specs) == 2


def test_turn_exception_evicts_session() -> None:
    failed = _FakeSession(results=[], error=ValueError("failed"))
    recovered = _FakeSession(results=[AgentTurnResult("ok")])
    driver = _FakeDriver([failed, recovered])
    client = AgentClient(driver)

    with pytest.raises(ValueError, match="failed"):
        client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=_key("impl"))
    result = client.run(
        session_spec=_spec(), turn=AgentTurnRequest("two"), session_key=_key("impl")
    )

    assert failed.close_calls == 1
    assert result.text == "ok"


def test_close_is_idempotent_and_rejects_future_turns() -> None:
    session = _FakeSession(results=[AgentTurnResult("ok")])
    driver = _FakeDriver([session])
    client = AgentClient(driver)
    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=_key("impl"))

    client.close()
    client.close()

    assert session.close_calls == 1
    assert driver.close_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        client.run(session_spec=_spec(), turn=AgentTurnRequest("two"))


def test_evicting_a_session_cancels_its_turn_before_closing_it() -> None:
    session = _FakeSession(results=[AgentTurnResult("ok")])
    client = AgentClient(_FakeDriver([session]))
    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=_key("impl"))

    client.close()

    # Order matters: a client closed from another thread must stop the turn
    # before releasing the resources that turn is still using.
    assert session.lifecycle_calls == ["cancel", "close"]


def test_a_failing_cancel_still_closes_the_session() -> None:
    session = _FakeSession(results=[AgentTurnResult("ok")], cancel_error=ValueError("no hook"))
    client = AgentClient(_FakeDriver([session]))
    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=_key("impl"))

    with pytest.raises(ValueError, match="no hook"):
        client.close()

    assert session.close_calls == 1


def test_invoke_builds_session_and_turn_contracts_and_records_usage(tmp_path: Path) -> None:
    usage = AgentUsage(input_tokens=12, output_tokens=4, total_cost_usd=0.02, duration_ms=30)
    session = _FakeSession(results=[AgentTurnResult('{"answer":"done"}', usage=usage)])
    driver = _FakeDriver([session])
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    client = AgentClient(
        driver,
        provider="codex",
        model_name="gpt-test",
        timeout=45,
        log_dir=log_dir,
        role_models={"judge": "gpt-judge"},
        role_reasoning_efforts={"judge": "high"},
    )

    response = client.invoke(
        kind="judge",
        workspace=tmp_path,
        system_prompt="system",
        user_prompt="user",
        response_cls=_Response,
        fallback_factory=lambda: _Response(answer="fallback"),
        round_label="judge #1",
        env={"VISIBLE": "1"},
        session_key=_key("review"),
    )

    assert response == _Response(answer="done")
    assert driver.specs[0].model == "gpt-judge"
    assert driver.specs[0].reasoning_effort == "high"
    assert driver.specs[0].environment == (("VISIBLE", "1"),)
    assert session.turns[0].instructions == "system"
    assert session.turns[0].message == "user"
    assert session.turns[0].output_schema is _Response
    assert session.turns[0].timeout == timedelta(seconds=45)
    record = json.loads((log_dir / "usage.jsonl").read_text())
    expected = {
        "kind": "judge",
        "round_label": "judge #1",
        "provider": "codex",
        "model": "gpt-judge",
        "reasoning_effort": "high",
        "input_tokens": 12,
        "output_tokens": 4,
        "total_cost_usd": 0.02,
        "duration_ms": 30,
    }
    assert {key: record[key] for key in expected} == expected


def test_runtime_accessors_expose_configured_driver_provider_and_model() -> None:
    client = AgentClient(
        _FakeDriver([]),
        driver_name="agentshim",
        provider="claude",
        model_name="claude-base",
        role_models={"judge": "claude-judge"},
    )

    assert client.driver_name == "agentshim"
    assert client.provider == "claude"
    assert client.model_for_kind("judge") == "claude-judge"
    assert client.model_for_kind("implementer") == "claude-base"


def test_runtime_accessors_default_to_none_or_codex_when_unconfigured() -> None:
    client = AgentClient(_FakeDriver([]))

    assert client.driver_name is None
    assert client.provider == "codex"
    assert client.model_for_kind("implementer") is None


def test_invoke_uses_fallback_only_for_unparseable_output(tmp_path: Path) -> None:
    session = _FakeSession(results=[AgentTurnResult("not json")])
    client = AgentClient(_FakeDriver([session]))
    fallback_calls = 0

    def fallback() -> _Response:
        nonlocal fallback_calls
        fallback_calls += 1
        return _Response(answer="fallback")

    response = client.invoke(
        kind="judge",
        workspace=tmp_path,
        system_prompt="system",
        user_prompt="user",
        response_cls=_Response,
        fallback_factory=fallback,
        round_label="judge #1",
    )

    assert response == _Response(answer="fallback")
    assert fallback_calls == 1
