"""Contract tests for the mock agent driver.

The mock is only useful if it reaches the rest of the system by the same
route a real driver does, so these tests assert on what an ``OutputSink``
subscriber observes while an ``AgentClient`` runs a turn, never on the
driver's internals.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  # pytest tmp_path annotation
from typing import TYPE_CHECKING

import pytest

from vibesys.agents.client import AgentClient
from vibesys.agents.contracts import AgentExecutionPolicy, AgentSessionSpec, AgentTurnRequest
from vibesys.agents.drivers.mock import (
    MockDriver,
    MockDriverError,
    ReplayPlaybook,
    ScriptedPlaybook,
)
from vibesys.render.sink import output_sink
from vibesys.run.events import (
    AgentOutputChunkData,
    CoreEvent,
    CoreEventType,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
)
from vibesys.schemas import OrchestratorPlan

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def sink_events() -> Iterator[list[CoreEvent]]:
    """Collect every event the process-global output sink publishes."""
    collected: list[CoreEvent] = []
    unsubscribe = output_sink().subscribe(collected.append)
    try:
        yield collected
    finally:
        unsubscribe()


def _invoke_plan(  # noqa: ANN202
    driver: MockDriver,
    workspace: Path,
    *,
    round_label: str = "round 1",
):
    """Run one orchestrator turn through the real client for ``driver``.

    The client is left open: the caller may run further turns on the same
    driver, and closing it would close the driver with it.
    """
    client = AgentClient(driver, driver_name="mock", provider="mock", model_name="mock-model")
    return client.invoke(
        kind="orchestrator",
        workspace=workspace,
        system_prompt="plan the round",
        user_prompt="what should we try?",
        response_cls=OrchestratorPlan,
        fallback_factory=lambda: OrchestratorPlan(
            hypothesis_id="",
            hypothesis="",
            task="",
            pass_criteria="",
            reasoning="fallback",
        ),
        round_label=round_label,
    )


def _of_type(events: list[CoreEvent], event_type: CoreEventType) -> list[CoreEvent]:
    return [event for event in events if event.type is event_type]


def test_scripted_mode_answers_a_structured_turn(tmp_path, sink_events):  # noqa: ANN001, ANN201, ARG001
    plan = _invoke_plan(MockDriver(ScriptedPlaybook()), tmp_path)

    assert isinstance(plan, OrchestratorPlan)
    assert plan.hypothesis_id == "H-01"
    assert plan.reasoning != "fallback", "the mock must produce a parseable structured answer"


def test_scripted_mode_publishes_the_whole_agent_event_vocabulary(tmp_path, sink_events):  # noqa: ANN001, ANN201
    playbook = ScriptedPlaybook(
        text_chunks=3,
        thinking_chunks=2,
        tool_calls=4,
        todo_updates=1,
        usage_updates=1,
        tool_result_chars=128,
    )

    _invoke_plan(MockDriver(playbook), tmp_path)

    assistant = [
        event
        for event in _of_type(sink_events, CoreEventType.AGENT_OUTPUT_CHUNK)
        if isinstance(event.data, AgentOutputChunkData) and event.data.channel == "assistant"
    ]
    analysis = [
        event
        for event in _of_type(sink_events, CoreEventType.AGENT_OUTPUT_CHUNK)
        if isinstance(event.data, AgentOutputChunkData) and event.data.channel == "analysis"
    ]
    tool_calls = [
        event
        for event in _of_type(sink_events, CoreEventType.TOOL_CALL)
        if isinstance(event.data, ToolCallData) and event.data.tool == "Bash"
    ]
    tool_results = _of_type(sink_events, CoreEventType.TOOL_RESULT)
    todos = _of_type(sink_events, CoreEventType.TODO_UPDATE)
    usage = _of_type(sink_events, CoreEventType.USAGE_UPDATE)

    assert len(assistant) >= playbook.text_chunks
    assert len(analysis) == playbook.thinking_chunks
    assert len(tool_calls) == playbook.tool_calls
    assert len(tool_results) == playbook.tool_calls
    assert len(todos) == playbook.todo_updates
    assert len(usage) == playbook.usage_updates


def test_scripted_tool_results_carry_the_configured_payload_size(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(MockDriver(ScriptedPlaybook(tool_calls=1, tool_result_chars=4096)), tmp_path)

    results = _of_type(sink_events, CoreEventType.TOOL_RESULT)
    assert len(results) == 1
    data = results[0].data
    assert isinstance(data, ToolResultData)
    assert len(data.content) == 4096
    assert data.is_error is False


def test_scripted_tool_calls_and_results_are_correlated(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(MockDriver(ScriptedPlaybook(tool_calls=3, todo_updates=0)), tmp_path)

    call_ids = [
        event.data.call_id
        for event in _of_type(sink_events, CoreEventType.TOOL_CALL)
        if isinstance(event.data, ToolCallData)
    ]
    result_ids = [
        event.data.call_id
        for event in _of_type(sink_events, CoreEventType.TOOL_RESULT)
        if isinstance(event.data, ToolResultData)
    ]

    assert all(call_id for call_id in call_ids)
    assert result_ids == call_ids


def test_scripted_todos_arrive_as_a_provider_plan_snapshot(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(MockDriver(ScriptedPlaybook(todo_updates=1, todo_items=3)), tmp_path)

    todos = _of_type(sink_events, CoreEventType.TODO_UPDATE)
    assert len(todos) == 1
    data = todos[0].data
    assert isinstance(data, TodoUpdateData)
    assert [item.status for item in data.todos] == ["in_progress", "pending", "pending"]


def test_scripted_usage_reaches_the_sink_as_a_usage_update(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(MockDriver(ScriptedPlaybook()), tmp_path)

    usage = _of_type(sink_events, CoreEventType.USAGE_UPDATE)
    assert usage, "a turn must report token usage"
    data = usage[-1].data
    assert isinstance(data, UsageUpdateData)
    assert data.input_tokens > 0
    assert data.model == "mock-model"


def test_scripted_rounds_advance_the_hypothesis_story(tmp_path, sink_events):  # noqa: ANN001, ANN201, ARG001
    driver = MockDriver(ScriptedPlaybook())

    first = _invoke_plan(driver, tmp_path, round_label="round 1")
    third = _invoke_plan(driver, tmp_path, round_label="round 3")

    assert first.hypothesis_id == "H-01"
    assert third.hypothesis_id == "H-02"


def test_events_are_scoped_to_the_invoking_role_and_round(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(MockDriver(ScriptedPlaybook()), tmp_path, round_label="round 7")

    scoped = [event for event in sink_events if event.type is CoreEventType.TOOL_CALL]
    assert scoped
    assert {event.agent_kind for event in scoped} == {"orchestrator"}
    assert {event.round_label for event in scoped} == {"round 7"}


def test_an_unscripted_response_schema_is_rejected_rather_than_faked(tmp_path):  # noqa: ANN001, ANN201
    from pydantic import BaseModel  # noqa: PLC0415

    class UnknownResponse(BaseModel):
        answer: str

    driver = MockDriver(ScriptedPlaybook(text_chunks=0, tool_calls=0, todo_updates=0))
    session = driver.create_session(
        AgentSessionSpec(
            role="orchestrator",
            provider="mock",
            workspace=tmp_path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )
    )

    with pytest.raises(MockDriverError, match="UnknownResponse"):
        session.run_turn(AgentTurnRequest(message="go", output_schema=UnknownResponse))


def test_cancel_is_idempotent_and_leaves_the_session_usable(tmp_path):  # noqa: ANN001, ANN201
    driver = MockDriver(ScriptedPlaybook())
    session = driver.create_session(
        AgentSessionSpec(
            role="orchestrator",
            provider="mock",
            workspace=tmp_path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )
    )

    session.cancel()
    session.cancel()

    # The mock has no in-flight turn to stop, so cancelling must not act like
    # close(): the next turn still runs.
    assert session.run_turn(AgentTurnRequest(message="go")).text


def test_a_closed_driver_refuses_new_sessions(tmp_path):  # noqa: ANN001, ANN201
    driver = MockDriver(ScriptedPlaybook())
    driver.close()
    driver.close()  # idempotent

    with pytest.raises(MockDriverError):
        driver.create_session(
            AgentSessionSpec(
                role="orchestrator",
                provider="mock",
                workspace=tmp_path,
                policy=AgentExecutionPolicy(require_enforcement=False),
            )
        )


def test_replay_mode_round_trips_a_recorded_event_log(tmp_path, sink_events):  # noqa: ANN001, ANN201
    recording = tmp_path / "run-events.jsonl"
    recorded = _record(MockDriver(ScriptedPlaybook(tool_calls=2, todo_updates=1)), tmp_path)
    recording.write_text("".join(event.model_dump_json() + "\n" for event in recorded))
    sink_events.clear()

    _invoke_plan(MockDriver(ReplayPlaybook(events_path=recording)), tmp_path)

    def shape(events: list[CoreEvent]) -> list[tuple[str, str]]:
        return [
            (event.type.value, _label(event))
            for event in events
            if event.type
            in {
                CoreEventType.TOOL_CALL,
                CoreEventType.TOOL_RESULT,
                CoreEventType.TODO_UPDATE,
                CoreEventType.USAGE_UPDATE,
            }
        ]

    assert shape(sink_events) == shape(recorded)


def test_replay_mode_reproduces_assistant_and_analysis_text(tmp_path, sink_events):  # noqa: ANN001, ANN201
    recording = tmp_path / "run-events.jsonl"
    recorded = _record(MockDriver(ScriptedPlaybook(text_chunks=3, thinking_chunks=2)), tmp_path)
    recording.write_text("".join(event.model_dump_json() + "\n" for event in recorded))
    sink_events.clear()

    _invoke_plan(MockDriver(ReplayPlaybook(events_path=recording)), tmp_path)

    def channels(events: list[CoreEvent]) -> list[str]:
        return [
            event.data.channel
            for event in events
            if event.type is CoreEventType.AGENT_OUTPUT_CHUNK
            and isinstance(event.data, AgentOutputChunkData)
            and event.data.channel in {"assistant", "analysis"}
        ]

    assert channels(sink_events) == channels(recorded)


def test_replay_mode_rejects_a_missing_recording(tmp_path):  # noqa: ANN001, ANN201
    driver = MockDriver(ReplayPlaybook(events_path=tmp_path / "absent.jsonl"))
    session = driver.create_session(
        AgentSessionSpec(
            role="orchestrator",
            provider="mock",
            workspace=tmp_path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )
    )

    with pytest.raises(MockDriverError, match="replay event log not found"):
        session.run_turn(AgentTurnRequest(message="go"))


def test_replay_mode_skips_a_truncated_trailing_record(tmp_path, sink_events):  # noqa: ANN001, ANN201
    recording = tmp_path / "run-events.jsonl"
    recorded = _record(MockDriver(ScriptedPlaybook(tool_calls=1, todo_updates=0)), tmp_path)
    body = "".join(event.model_dump_json() + "\n" for event in recorded)
    recording.write_text(body + '{"type": "tool_call", "timestamp"')
    sink_events.clear()

    _invoke_plan(MockDriver(ReplayPlaybook(events_path=recording)), tmp_path)

    assert _of_type(sink_events, CoreEventType.TOOL_CALL)


def _label(event: CoreEvent) -> str:
    data = event.data
    if isinstance(data, ToolCallData):
        return data.tool
    if isinstance(data, ToolResultData):
        return data.content
    if isinstance(data, TodoUpdateData):
        return ",".join(f"{item.content}:{item.status}" for item in data.todos)
    if isinstance(data, UsageUpdateData):
        return str(data.input_tokens)
    return ""


def _record(driver: MockDriver, workspace: Path) -> list[CoreEvent]:
    """Capture one scripted turn's sink events as a run-events recording."""
    collected: list[CoreEvent] = []
    unsubscribe = output_sink().subscribe(collected.append)
    try:
        _invoke_plan(driver, workspace)
    finally:
        unsubscribe()
    return collected
