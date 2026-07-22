"""Serialization tests for the run-event wire contract."""

import pytest
from pydantic import ValidationError

from vibesys.server.events import (
    AgentOutputChunkData,
    AgentStatusData,
    BenchmarkResultData,
    EventType,
    RoundFinishedData,
    RunEvent,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
    make_event,
)


def _round_trip(event: RunEvent) -> RunEvent:
    return RunEvent.model_validate_json(event.model_dump_json())


class TestNewEventDataRoundTrip:
    def test_tool_call(self):
        status = AgentStatusData(
            progress="Round 1/2",
            agent_label="Implementer",
            elapsed_seconds=1.5,
            input_tokens=1000,
            context_window=200_000,
        )
        event = make_event(
            EventType.TOOL_CALL,
            data=ToolCallData(tool="shell", args={"cmd": "ls", "count": 3}, status=status),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, ToolCallData)
        assert restored.data.tool == "shell"
        assert restored.data.args == {"cmd": "ls", "count": 3}
        assert restored.data.status == status

    def test_tool_result(self):
        event = make_event(
            EventType.TOOL_RESULT,
            data=ToolResultData(tool="shell", content="out", is_error=True),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, ToolResultData)
        assert restored.data.is_error is True

    def test_todo_update(self):
        event = make_event(
            EventType.TODO_UPDATE,
            data=TodoUpdateData(todos=[TodoItemData(content="a", status="pending")]),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, TodoUpdateData)
        assert restored.data.todos == [TodoItemData(content="a", status="pending")]

    def test_usage_update(self):
        event = make_event(
            EventType.USAGE_UPDATE,
            data=UsageUpdateData(input_tokens=5_000, context_window=1_000_000, model="m"),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, UsageUpdateData)
        assert restored.data.input_tokens == 5_000

    def test_agent_output_chunk_status_is_optional_and_round_trips(self):
        bare = make_event(
            EventType.AGENT_OUTPUT_CHUNK,
            data=AgentOutputChunkData(channel="assistant", content="hi"),
        )
        restored = _round_trip(bare)
        assert isinstance(restored.data, AgentOutputChunkData)
        assert restored.data.status is None

        status = AgentStatusData(agent_label="Judge", elapsed_seconds=0.5, input_tokens=10)
        rich = make_event(
            EventType.AGENT_OUTPUT_CHUNK,
            data=AgentOutputChunkData(channel="assistant", content="hi", status=status),
        )
        restored = _round_trip(rich)
        assert isinstance(restored.data, AgentOutputChunkData)
        assert restored.data.status == status


class TestBackwardCompatibility:
    def test_chunk_without_status_field_still_parses(self):
        """Events recorded by older backends omit the new optional fields."""
        raw = (
            '{"protocol_version": 1, "sequence": 3, "run_id": "r", '
            '"timestamp": "2026-01-01T00:00:00Z", "type": "agent_output_chunk", '
            '"data": {"kind": "agent_output_chunk", "channel": "tool", "content": "x"}}'
        )
        event = RunEvent.model_validate_json(raw)
        assert isinstance(event.data, AgentOutputChunkData)
        assert event.data.status is None

    def test_unknown_data_kind_rejected(self):
        raw = (
            '{"protocol_version": 1, "timestamp": "2026-01-01T00:00:00Z", '
            '"type": "output", "data": {"kind": "not_a_kind"}}'
        )
        with pytest.raises(ValueError):
            RunEvent.model_validate_json(raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_benchmark_result_rejects_non_finite_value(value):
    with pytest.raises(ValidationError, match="finite number"):
        BenchmarkResultData(metric="throughput", value=value, unit="req/s")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_round_finished_rejects_non_finite_perf_metric(value):
    with pytest.raises(ValidationError, match="finite number"):
        RoundFinishedData(
            attempts=1,
            judge_verdict="pass",
            perf_metric=value,
            perf_unit="req/s",
        )
