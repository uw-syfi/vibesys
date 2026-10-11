"""Public behavior tests for run-scoped agent event translation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import (
    AgentOutputChunkData,
    AgentStatusData,
    CommandResultPayload,
    CoreEventType,
    JsonResultPayload,
    RetryWaitData,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    TurnUsageData,
    UsageUpdateData,
)
from vibesys.run import CoreAgentEventSink, EventJournal
from vs_agent.api import AgentEventSink

if TYPE_CHECKING:
    from pathlib import Path


def _attached_sink(tmp_path: Path, run_id: str) -> tuple[EventJournal, CoreAgentEventSink]:
    journal = EventJournal()
    journal.attach(tmp_path / run_id, run_id)
    return journal, CoreAgentEventSink(journal.record)


def test_adapter_implements_public_agent_event_sink(tmp_path: Path) -> None:
    _, sink = _attached_sink(tmp_path, "run-1")
    public_sink: AgentEventSink = sink

    assert isinstance(public_sink, AgentEventSink)


def test_each_callback_records_one_typed_event_with_routing_metadata(tmp_path: Path) -> None:
    journal, sink = _attached_sink(tmp_path, "run-1")
    status = AgentStatusData(progress="Round 2/3", input_tokens=42)

    sink.agent_output(
        "working",
        channel="analysis",
        status=status,
        agent_kind="implementer",
        round_label="round-2",
        invocation_id="invocation-7",
    )
    sink.tool_call(
        "write",
        {"path": tmp_path / "candidate.py"},
        call_id="call-1",
        status=status,
        agent_kind="implementer",
        round_label="round-2",
        invocation_id="invocation-7",
    )
    sink.tool_result(
        "write",
        '{"written": true}',
        call_id="call-1",
        agent_kind="implementer",
        round_label="round-2",
        invocation_id="invocation-7",
    )
    sink.todo_update(
        [TodoItemData(content="Run tests", status="pending")],
        agent_kind="implementer",
        round_label="round-2",
        invocation_id="invocation-7",
    )
    sink.usage_update(
        1234,
        context_window=8192,
        model="test-model",
        agent_kind="implementer",
        round_label="round-2",
        invocation_id="invocation-7",
    )

    events = journal.read()
    assert [event.type for event in events] == [
        CoreEventType.AGENT_OUTPUT_CHUNK,
        CoreEventType.TOOL_CALL,
        CoreEventType.TOOL_RESULT,
        CoreEventType.TODO_UPDATE,
        CoreEventType.USAGE_UPDATE,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert {event.run_id for event in events} == {"run-1"}
    assert {event.agent_kind for event in events} == {"implementer"}
    assert {event.round_label for event in events} == {"round-2"}
    assert {event.execution_id for event in events} == {"invocation-7"}

    output = events[0].data
    assert output == AgentOutputChunkData(channel="analysis", content="working", status=status)
    call = events[1].data
    assert isinstance(call, ToolCallData)
    assert call.call_id == "call-1"
    assert call.status == status
    assert isinstance(call.args["path"], str)
    result = events[2].data
    assert isinstance(result, ToolResultData)
    assert result.call_id == "call-1"
    assert result.payload == JsonResultPayload(value={"written": True})
    assert events[3].data == TodoUpdateData(
        todos=[TodoItemData(content="Run tests", status="pending")]
    )
    assert events[4].data == UsageUpdateData(
        input_tokens=1234,
        context_window=8192,
        model="test-model",
    )


def test_retry_wait_records_one_typed_event_with_no_routing_metadata(tmp_path: Path) -> None:
    """The retry loop reports a wait before per-turn routing context is bound.

    Unlike every other ``AgentEventSink`` callback, ``retry_wait`` carries no
    ``agent_kind``/``round_label``/``invocation_id``: the retry loop wraps a
    whole turn, before the turn that will eventually bind that context even
    starts.
    """
    journal, sink = _attached_sink(tmp_path, "run-1")

    sink.retry_wait(2, 4.5, "overloaded")

    events = journal.read()
    assert [event.type for event in events] == [CoreEventType.RETRY_WAIT_STARTED]
    event = events[0]
    assert event.agent_kind is None
    assert event.round_label is None
    assert event.execution_id is None
    assert event.data == RetryWaitData(attempt=2, delay_s=4.5, error_kind="overloaded")


def test_turn_usage_records_one_typed_event_with_routing_metadata(tmp_path: Path) -> None:
    journal, sink = _attached_sink(tmp_path, "run-1")

    sink.turn_usage(
        input_tokens=11,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=22,
        total_cost_usd=0.03,
        duration_ms=1500,
        agent_kind="dynamic-implementer",
        round_label="round-1",
        invocation_id="invocation-1",
    )

    events = journal.read()
    assert [event.type for event in events] == [CoreEventType.TURN_USAGE_RECORDED]
    event = events[0]
    assert event.agent_kind == "dynamic-implementer"
    assert event.round_label == "round-1"
    assert event.execution_id == "invocation-1"
    assert event.data == TurnUsageData(
        input_tokens=11,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=22,
        total_cost_usd=0.03,
        duration_ms=1500,
    )


def test_explicit_tool_result_payload_is_preserved(tmp_path: Path) -> None:
    journal, sink = _attached_sink(tmp_path, "run-1")
    payload = CommandResultPayload(stdout='{"written": true}', stderr="", exit_code=0)

    sink.tool_result("shell", payload.stdout, payload=payload, is_error=True)

    event = journal.read()[0]
    assert isinstance(event.data, ToolResultData)
    assert event.data.payload == payload
    assert event.data.is_error is True


def test_adapters_are_isolated_by_their_injected_run_journals(tmp_path: Path) -> None:
    first_journal, first_sink = _attached_sink(tmp_path, "run-1")
    second_journal, second_sink = _attached_sink(tmp_path, "run-2")

    first_sink.agent_output("first", agent_kind="judge")
    second_sink.agent_output("second", agent_kind="implementer")
    first_sink.usage_update(10)

    assert [event.type for event in first_journal.read()] == [
        CoreEventType.AGENT_OUTPUT_CHUNK,
        CoreEventType.USAGE_UPDATE,
    ]
    assert [event.run_id for event in first_journal.read()] == ["run-1", "run-1"]
    assert [event.type for event in second_journal.read()] == [CoreEventType.AGENT_OUTPUT_CHUNK]
    assert [event.run_id for event in second_journal.read()] == ["run-2"]
