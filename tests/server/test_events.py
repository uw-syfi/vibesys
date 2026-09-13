"""Serialization tests for the run-event wire contract."""

import pytest
from pydantic import ValidationError

from server.diagnostics import Diagnostic, DiagnosticScope, DiagnosticSeverity
from server.events import (
    AgentOutputChunkData,
    AgentStatusData,
    BenchmarkResultData,
    CommandResultPayload,
    EventStatus,
    EventType,
    FrameworkSource,
    FrameworkWarningData,
    GateFinishedData,
    GateKind,
    GateStartedData,
    JsonResultPayload,
    RoundFinishedData,
    RunConfiguredData,
    RunEvent,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
    WorkspaceSnapshotData,
    make_event,
)


def _round_trip(event: RunEvent) -> RunEvent:
    return RunEvent.model_validate_json(event.model_dump_json())


class TestNewEventDataRoundTrip:
    def test_tool_call(self):  # noqa: ANN201  # tracked: #288
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

    def test_tool_result(self):  # noqa: ANN201  # tracked: #288
        event = make_event(
            EventType.TOOL_RESULT,
            data=ToolResultData(tool="shell", content="out", is_error=True),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, ToolResultData)
        assert restored.data.is_error is True
        assert restored.data.payload is None

    def test_tool_result_with_command_payload(self):  # noqa: ANN201  # tracked: #288
        payload = CommandResultPayload(stdout="out", stderr="warn", exit_code=2, duration=1.5)
        event = make_event(
            EventType.TOOL_RESULT,
            data=ToolResultData(tool="shell", content="out", payload=payload),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, ToolResultData)
        assert isinstance(restored.data.payload, CommandResultPayload)
        assert restored.data.payload == payload
        assert restored.data.content == "out"

    def test_tool_result_with_json_payload(self):  # noqa: ANN201  # tracked: #288
        payload = JsonResultPayload(value={"rows": [1, 2], "ok": True})
        event = make_event(
            EventType.TOOL_RESULT,
            data=ToolResultData(
                tool="query", content='{"rows": [1, 2], "ok": true}', payload=payload
            ),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, ToolResultData)
        assert isinstance(restored.data.payload, JsonResultPayload)
        assert restored.data.payload.value == {"rows": [1, 2], "ok": True}

    def test_tool_result_payload_rejects_unknown_kind(self):  # noqa: ANN201  # tracked: #288
        with pytest.raises(ValidationError):
            ToolResultData.model_validate(
                {"tool": "shell", "content": "out", "payload": {"kind": "mystery"}}
            )

    def test_json_payload_rejects_scalar_value(self):  # noqa: ANN201  # tracked: #288
        with pytest.raises(ValidationError):
            JsonResultPayload.model_validate({"value": 42})

    def test_tool_result_without_payload_field_still_validates(self):  # noqa: ANN201  # tracked: #288
        # Old event logs predate the payload field and must keep replaying.
        restored = ToolResultData.model_validate(
            {"kind": "tool_result", "tool": "t", "content": "c"}
        )
        assert restored.payload is None

    def test_todo_update(self):  # noqa: ANN201  # tracked: #288
        event = make_event(
            EventType.TODO_UPDATE,
            data=TodoUpdateData(todos=[TodoItemData(content="a", status="pending")]),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, TodoUpdateData)
        assert restored.data.todos == [TodoItemData(content="a", status="pending")]

    def test_usage_update(self):  # noqa: ANN201  # tracked: #288
        event = make_event(
            EventType.USAGE_UPDATE,
            data=UsageUpdateData(input_tokens=5_000, context_window=1_000_000, model="m"),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, UsageUpdateData)
        assert restored.data.input_tokens == 5_000

    def test_agent_output_chunk_status_is_optional_and_round_trips(self):  # noqa: ANN201  # tracked: #288
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
    def test_chunk_without_status_field_still_parses(self):  # noqa: ANN201  # tracked: #288
        """Events recorded by older backends omit the new optional fields."""
        raw = (
            '{"protocol_version": 1, "sequence": 3, "run_id": "r", '
            '"timestamp": "2026-01-01T00:00:00Z", "type": "agent_output_chunk", '
            '"data": {"kind": "agent_output_chunk", "channel": "tool", "content": "x"}}'
        )
        event = RunEvent.model_validate_json(raw)
        assert isinstance(event.data, AgentOutputChunkData)
        assert event.data.status is None

    def test_unknown_data_kind_rejected(self):  # noqa: ANN201  # tracked: #288
        raw = (
            '{"protocol_version": 1, "timestamp": "2026-01-01T00:00:00Z", '
            '"type": "output", "data": {"kind": "not_a_kind"}}'
        )
        with pytest.raises(ValueError):  # noqa: PT011  # tracked: #288
            RunEvent.model_validate_json(raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_benchmark_result_rejects_non_finite_value(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValidationError, match="finite number"):
        BenchmarkResultData(metric="throughput", value=value, unit="req/s")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_round_finished_rejects_non_finite_perf_metric(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValidationError, match="finite number"):
        RoundFinishedData(
            attempts=1,
            judge_verdict="pass",
            perf_metric=value,
            perf_unit="req/s",
        )


class TestRoundFinishedProfileSkipped:
    def test_flag_round_trips_when_true(self):  # noqa: ANN201  # tracked: #288
        event = make_event(
            EventType.ROUND_FINISHED,
            data=RoundFinishedData(
                attempts=1,
                judge_verdict="pass",
                perf_metric=100.0,
                perf_unit="req/s",
                profile_skipped=True,
            ),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, RoundFinishedData)
        assert restored.data.profile_skipped is True

    def test_payload_without_flag_defaults_false(self):  # noqa: ANN201  # tracked: #288
        """Events recorded before the field existed must keep replaying."""
        raw = (
            '{"protocol_version": 1, "sequence": 7, "run_id": "r", '
            '"timestamp": "2026-01-01T00:00:00Z", "type": "round_finished", '
            '"data": {"kind": "round_finished", "attempts": 2, "judge_verdict": "pass", '
            '"perf_metric": 100.0, "perf_unit": "req/s"}}'
        )
        event = RunEvent.model_validate_json(raw)
        assert isinstance(event.data, RoundFinishedData)
        assert event.data.profile_skipped is False


class TestFrameworkEventRoundTrip:
    def test_gate_started(self):  # noqa: ANN201
        event = make_event(
            EventType.GATE_STARTED,
            status=EventStatus.ACTIVE,
            round_label="round-3",
            data=GateStartedData(
                gate=GateKind.VALIDATION,
                recipe="focused-tests",
                command="uv run pytest -q",
            ),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, GateStartedData)
        assert restored.data.gate is GateKind.VALIDATION
        assert restored.data.recipe == "focused-tests"
        assert restored.data.command == "uv run pytest -q"
        assert restored.data.source is FrameworkSource.GATES
        assert restored.status is EventStatus.ACTIVE
        assert restored.round_label == "round-3"

    def test_gate_finished_pass_with_metric(self):  # noqa: ANN201
        event = make_event(
            EventType.GATE_FINISHED,
            status=EventStatus.COMPLETED,
            data=GateFinishedData(
                gate=GateKind.BENCHMARK,
                metric="tok_per_sec",
                value=42.5,
                unit="tok/s",
            ),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, GateFinishedData)
        assert restored.data.gate is GateKind.BENCHMARK
        assert restored.data.metric == "tok_per_sec"
        assert restored.data.value == 42.5
        assert restored.data.unit == "tok/s"
        assert restored.data.reused is False
        assert restored.status is EventStatus.COMPLETED

    def test_gate_finished_failure_with_output_tail(self):  # noqa: ANN201
        event = make_event(
            EventType.GATE_FINISHED,
            status=EventStatus.FAILED,
            data=GateFinishedData(
                gate=GateKind.ACCURACY,
                output_tail="assertion mismatch",
            ),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, GateFinishedData)
        assert restored.data.output_tail == "assertion mismatch"
        assert restored.status is EventStatus.FAILED
        # A failed gate is an expected outcome, not a run fault: no diagnostic.
        assert restored.diagnostic is None

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_gate_finished_rejects_non_finite_value(self, value):  # noqa: ANN001, ANN201
        with pytest.raises(ValidationError, match="finite"):
            GateFinishedData(gate=GateKind.BENCHMARK, metric="m", value=value)

    def test_workspace_snapshot(self):  # noqa: ANN201
        event = make_event(
            EventType.WORKSPACE_SNAPSHOT,
            data=WorkspaceSnapshotData(label="round-2", commit="a" * 40),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, WorkspaceSnapshotData)
        assert restored.data.label == "round-2"
        assert restored.data.commit == "a" * 40
        assert restored.data.baseline is None
        assert restored.data.excluded_paths == ()
        assert restored.data.source is FrameworkSource.GIT_TRACKING

    def test_run_configured(self):  # noqa: ANN201
        event = make_event(
            EventType.RUN_CONFIGURED,
            data=RunConfiguredData(
                run_log_path="/logs/run-1.log",
                project_root="/work/project",
                model="claude-sonnet-4-6",
                objective="Make the queue fast.",
                search_policy="pareto-ucb",
                benchmark_contract=True,
                pareto_objectives="[latency(min)], frontier_bias=0.5",
            ),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, RunConfiguredData)
        assert restored.data.run_log_path == "/logs/run-1.log"
        assert restored.data.search_policy == "pareto-ucb"
        assert restored.data.benchmark_contract is True

    def test_framework_warning_with_diagnostic(self):  # noqa: ANN201
        """The projection lifts the payload into the wire diagnostic field."""
        event = make_event(
            EventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary="profiler failed",
                detail="boom",
                source=FrameworkSource.LOOP,
            ),
            diagnostic=Diagnostic(
                code="framework_warning",
                summary="profiler failed",
                detail="boom",
                scope=DiagnosticScope.RUN,
                severity=DiagnosticSeverity.WARNING,
                source="loop",
            ),
        )
        restored = _round_trip(event)
        assert isinstance(restored.data, FrameworkWarningData)
        assert restored.data.summary == "profiler failed"
        assert restored.data.source is FrameworkSource.LOOP
        assert restored.diagnostic is not None
        assert restored.diagnostic.severity is DiagnosticSeverity.WARNING
        assert restored.diagnostic.source == "loop"

    def test_diagnostic_without_source_still_parses(self):  # noqa: ANN201
        """`source` is additive: persisted diagnostics without it stay valid."""
        raw = '{"code": "framework_warning", "summary": "s", "scope": "run", "severity": "warning"}'
        diagnostic = Diagnostic.model_validate_json(raw)
        assert diagnostic.source is None
