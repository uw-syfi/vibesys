"""Contract tests for the fake agent driver.

The fake is only useful if it reaches the rest of the system by the same
route a real driver does, so these tests assert on explicitly collected core
events while an ``AgentClient`` runs a turn, never on the driver's internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import (
    BaseModel,
)

from vibesys.api import CoreAgentEventSink
from vibesys.events import (
    AgentOutputChunkData,
    CoreEvent,
    CoreEventType,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
)
from vibesys.hypothesis import OrchestratorPlan
from vs_agent.api import NULL_AGENT_EVENT_SINK, AgentClient
from vs_agent.contracts import AgentExecutionPolicy, AgentSessionSpec, AgentTurnRequest
from vs_agent.drivers.fake import (
    FakeDriver,
    FakeDriverError,
    assistant_text,
    thinking,
    todo_write,
    tool_call,
    tool_result,
)
from vs_agent.drivers.fake import (
    usage as usage_event,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def sink_events() -> list[CoreEvent]:
    """Own the events emitted by one fake-driver test."""
    return []


def _invoke_plan(
    driver: FakeDriver,
    workspace: Path,
    *,
    round_label: str = "round 1",
    events: list[CoreEvent] | None = None,
) -> OrchestratorPlan:
    """Run one orchestrator turn through the real client for ``driver``.

    The client is left open: the caller may run further turns on the same
    driver, and closing it would close the driver with it.
    """
    event_sink = NULL_AGENT_EVENT_SINK if events is None else CoreAgentEventSink(events.append)
    client = AgentClient(
        driver,
        driver_name="mock",
        provider="mock",
        model_name="mock-model",
        event_sink=event_sink,
    )
    return client.invoke(
        kind="orchestrator",
        workspace=workspace,
        system_prompt="plan the round",
        user_prompt="what should we try?",
        response_cls=OrchestratorPlan,
        round_label=round_label,
    )


def _of_type(events: list[CoreEvent], event_type: CoreEventType) -> list[CoreEvent]:
    return [event for event in events if event.type is event_type]


def _plan_answer(hypothesis_id: str = "H-01") -> OrchestratorPlan:
    criteria = "the structured answer parses"
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="explicit fake hypothesis",
        task="exercise the fake driver",
        pass_criteria=criteria,
        reasoning="explicit fake answer",
    )


def test_scripted_mode_answers_a_structured_turn(tmp_path: Path) -> None:
    plan = _invoke_plan(
        FakeDriver(turn=[assistant_text("planning...")], answer=_plan_answer()),
        tmp_path,
    )

    assert isinstance(plan, OrchestratorPlan)
    assert plan.hypothesis_id == "H-01"


def test_scripted_mode_publishes_the_whole_agent_event_vocabulary(
    tmp_path: Path, sink_events: list[CoreEvent]
) -> None:
    text_chunks = 3
    thinking_chunks = 2
    tool_calls = 4
    todo_updates = 1
    usage_updates = 1

    events = (
        [thinking(f"reasoning {i}") for i in range(thinking_chunks)]
        + [assistant_text(f"chunk {i}") for i in range(text_chunks)]
        + [todo_write([("write spsc", "in_progress")])] * todo_updates
        + [
            event
            for i in range(tool_calls)
            for event in (
                tool_call("Bash", {"command": f"cmd-{i}"}),
                tool_result("x" * 128),
            )
        ]
        + [usage_event(input_tokens=1200, output_tokens=300)] * usage_updates
    )

    _invoke_plan(FakeDriver(turn=events, answer=_plan_answer()), tmp_path, events=sink_events)

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
    seen_tool_calls = [
        event
        for event in _of_type(sink_events, CoreEventType.TOOL_CALL)
        if isinstance(event.data, ToolCallData) and event.data.tool == "Bash"
    ]
    tool_results = _of_type(sink_events, CoreEventType.TOOL_RESULT)
    todos = _of_type(sink_events, CoreEventType.TODO_UPDATE)
    usage = _of_type(sink_events, CoreEventType.USAGE_UPDATE)

    assert len(assistant) >= text_chunks
    assert len(analysis) == thinking_chunks
    assert len(seen_tool_calls) == tool_calls
    assert len(tool_results) == tool_calls
    assert len(todos) == todo_updates
    assert len(usage) == usage_updates


def test_scripted_tool_results_carry_the_configured_payload_size(
    tmp_path: Path, sink_events: list[CoreEvent]
) -> None:
    _invoke_plan(
        FakeDriver(
            turn=[tool_call("Bash", {"command": "x"}), tool_result("y" * 4096)],
            answer=_plan_answer(),
        ),
        tmp_path,
        events=sink_events,
    )

    results = _of_type(sink_events, CoreEventType.TOOL_RESULT)
    assert len(results) == 1
    data = results[0].data
    assert isinstance(data, ToolResultData)
    assert len(data.content) == 4096
    assert data.is_error is False


def test_scripted_tool_calls_and_results_are_correlated(
    tmp_path: Path, sink_events: list[CoreEvent]
) -> None:
    events = [
        event
        for i in range(3)
        for event in (tool_call("Bash", {"command": f"cmd-{i}"}), tool_result(f"out-{i}"))
    ]

    _invoke_plan(FakeDriver(turn=events, answer=_plan_answer()), tmp_path, events=sink_events)

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


def test_scripted_todos_arrive_as_a_provider_plan_snapshot(
    tmp_path: Path, sink_events: list[CoreEvent]
) -> None:
    _invoke_plan(
        FakeDriver(
            turn=[
                todo_write(
                    [
                        ("round step 1", "in_progress"),
                        ("round step 2", "pending"),
                        ("round step 3", "pending"),
                    ]
                )
            ],
            answer=_plan_answer(),
        ),
        tmp_path,
        events=sink_events,
    )

    todos = _of_type(sink_events, CoreEventType.TODO_UPDATE)
    assert len(todos) == 1
    data = todos[0].data
    assert isinstance(data, TodoUpdateData)
    assert [item.status for item in data.todos] == ["in_progress", "pending", "pending"]


def test_scripted_usage_reaches_the_sink_as_a_usage_update(
    tmp_path: Path, sink_events: list[CoreEvent]
) -> None:
    _invoke_plan(
        FakeDriver(
            turn=[usage_event(input_tokens=1000, output_tokens=120)],
            answer=_plan_answer(),
        ),
        tmp_path,
        events=sink_events,
    )

    usage = _of_type(sink_events, CoreEventType.USAGE_UPDATE)
    assert usage, "a turn must report token usage"
    data = usage[-1].data
    assert isinstance(data, UsageUpdateData)
    assert data.input_tokens > 0
    assert data.model == "mock-model"


def test_explicit_structured_answer_is_reused_across_turns(tmp_path: Path) -> None:
    driver = FakeDriver(
        turn=[assistant_text("planning...")],
        answer=_plan_answer("configured"),
    )

    first = _invoke_plan(driver, tmp_path, round_label="round 1")
    third = _invoke_plan(driver, tmp_path, round_label="round 3")

    assert first.hypothesis_id == "configured"
    assert third.hypothesis_id == "configured"


def test_events_are_scoped_to_the_invoking_role_and_round(
    tmp_path: Path, sink_events: list[CoreEvent]
) -> None:
    _invoke_plan(
        FakeDriver(
            turn=[tool_call("Bash", {"command": "x"}), tool_result("ok")],
            answer=_plan_answer(),
        ),
        tmp_path,
        round_label="round 7",
        events=sink_events,
    )

    scoped = [event for event in sink_events if event.type is CoreEventType.TOOL_CALL]
    assert scoped
    assert {event.agent_kind for event in scoped} == {"orchestrator"}
    assert {event.round_label for event in scoped} == {"round 7"}


def test_an_unscripted_response_schema_is_rejected_rather_than_faked(tmp_path: Path) -> None:

    class UnknownResponse(BaseModel):
        answer: str

    driver = FakeDriver()
    session = driver.create_session(
        AgentSessionSpec(
            role="orchestrator",
            provider="mock",
            workspace=tmp_path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )
    )

    with pytest.raises(FakeDriverError, match="UnknownResponse"):
        session.run_turn(AgentTurnRequest(message="go", output_schema=UnknownResponse))


def test_cancel_is_idempotent_and_leaves_the_session_usable(tmp_path: Path) -> None:
    driver = FakeDriver()
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

    # The fake has no in-flight turn to stop, so cancelling must not act like
    # close(): the next turn still runs.
    assert session.run_turn(AgentTurnRequest(message="go")).text


def test_a_closed_driver_refuses_new_sessions(tmp_path: Path) -> None:
    driver = FakeDriver()
    driver.close()
    driver.close()  # idempotent

    with pytest.raises(FakeDriverError):
        driver.create_session(
            AgentSessionSpec(
                role="orchestrator",
                provider="mock",
                workspace=tmp_path,
                policy=AgentExecutionPolicy(require_enforcement=False),
            )
        )
