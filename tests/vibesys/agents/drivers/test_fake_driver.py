"""Contract tests for the fake agent driver.

The fake is only useful if it reaches the rest of the system by the same
route a real driver does, so these tests assert on what an ``OutputSink``
subscriber observes while an ``AgentClient`` runs a turn, never on the
driver's internals.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  # pytest tmp_path annotation
from typing import TYPE_CHECKING

import pytest

from vibesys.events import (
    AgentOutputChunkData,
    CoreEvent,
    CoreEventType,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
)
from vibesys.render.sink import output_sink
from vibesys.schemas import OrchestratorPlan
from vs_agent.api import AgentClient
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
    driver: FakeDriver,
    workspace: Path,
    *,
    round_label: str = "round 1",
):
    """Run one orchestrator turn through the real client for ``driver``.

    The client is left open: the caller may run further turns on the same
    driver, and closing it would close the driver with it.
    """
    client = AgentClient(
        driver,
        driver_name="mock",
        provider="mock",
        model_name="mock-model",
        event_sink=output_sink(),
    )
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
    plan = _invoke_plan(FakeDriver(turn=[assistant_text("planning...")]), tmp_path)

    assert isinstance(plan, OrchestratorPlan)
    assert plan.hypothesis_id == "H-01"
    assert plan.reasoning != "fallback", "the fake must produce a parseable structured answer"


def test_scripted_mode_publishes_the_whole_agent_event_vocabulary(tmp_path, sink_events):  # noqa: ANN001, ANN201
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

    _invoke_plan(FakeDriver(turn=events), tmp_path)

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


def test_scripted_tool_results_carry_the_configured_payload_size(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(
        FakeDriver(turn=[tool_call("Bash", {"command": "x"}), tool_result("y" * 4096)]),
        tmp_path,
    )

    results = _of_type(sink_events, CoreEventType.TOOL_RESULT)
    assert len(results) == 1
    data = results[0].data
    assert isinstance(data, ToolResultData)
    assert len(data.content) == 4096
    assert data.is_error is False


def test_scripted_tool_calls_and_results_are_correlated(tmp_path, sink_events):  # noqa: ANN001, ANN201
    events = [
        event
        for i in range(3)
        for event in (tool_call("Bash", {"command": f"cmd-{i}"}), tool_result(f"out-{i}"))
    ]

    _invoke_plan(FakeDriver(turn=events), tmp_path)

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
            ]
        ),
        tmp_path,
    )

    todos = _of_type(sink_events, CoreEventType.TODO_UPDATE)
    assert len(todos) == 1
    data = todos[0].data
    assert isinstance(data, TodoUpdateData)
    assert [item.status for item in data.todos] == ["in_progress", "pending", "pending"]


def test_scripted_usage_reaches_the_sink_as_a_usage_update(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(FakeDriver(turn=[usage_event(input_tokens=1000, output_tokens=120)]), tmp_path)

    usage = _of_type(sink_events, CoreEventType.USAGE_UPDATE)
    assert usage, "a turn must report token usage"
    data = usage[-1].data
    assert isinstance(data, UsageUpdateData)
    assert data.input_tokens > 0
    assert data.model == "mock-model"


def test_scripted_rounds_advance_the_hypothesis_story(tmp_path, sink_events):  # noqa: ANN001, ANN201, ARG001
    driver = FakeDriver(turn=[assistant_text("planning...")])

    first = _invoke_plan(driver, tmp_path, round_label="round 1")
    third = _invoke_plan(driver, tmp_path, round_label="round 3")

    assert first.hypothesis_id == "H-01"
    assert third.hypothesis_id == "H-02"


def test_events_are_scoped_to_the_invoking_role_and_round(tmp_path, sink_events):  # noqa: ANN001, ANN201
    _invoke_plan(
        FakeDriver(turn=[tool_call("Bash", {"command": "x"}), tool_result("ok")]),
        tmp_path,
        round_label="round 7",
    )

    scoped = [event for event in sink_events if event.type is CoreEventType.TOOL_CALL]
    assert scoped
    assert {event.agent_kind for event in scoped} == {"orchestrator"}
    assert {event.round_label for event in scoped} == {"round 7"}


def test_an_unscripted_response_schema_is_rejected_rather_than_faked(tmp_path):  # noqa: ANN001, ANN201
    from pydantic import BaseModel  # noqa: PLC0415

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


def test_cancel_is_idempotent_and_leaves_the_session_usable(tmp_path):  # noqa: ANN001, ANN201
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


def test_a_closed_driver_refuses_new_sessions(tmp_path):  # noqa: ANN001, ANN201
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
