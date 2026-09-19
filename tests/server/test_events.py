"""Serialization tests for the run-event wire contract."""

import json

import pytest

from server.diagnostics import (
    DiagnosticScope,
    DiagnosticSeverity,
    make_diagnostic,
)
from server.events import parse_event
from server.wire import codec, messages, validate
from server.wire.v2 import common_pb2, events_pb2, snapshot_pb2

ET = events_pb2.EventType
ES = events_pb2.EventStatus
GATES = events_pb2.FrameworkSource.FRAMEWORK_SOURCE_GATES
LOOP = events_pb2.FrameworkSource.FRAMEWORK_SOURCE_LOOP


def _round_trip(event: events_pb2.RunEvent) -> events_pb2.RunEvent:
    return codec.loads(events_pb2.RunEvent, codec.dumps(event))


class TestNewEventDataRoundTrip:
    def test_tool_call(self):  # noqa: ANN201  # tracked: #288
        status = events_pb2.AgentStatusData(
            progress="Round 1/2",
            agent_label="Implementer",
            elapsed_seconds=1.5,
            input_tokens=1000,
            context_window=200_000,
        )
        event = messages.make_event(
            ET.EVENT_TYPE_TOOL_CALL,
            data=events_pb2.ToolCallData(
                tool="shell", args={"cmd": "ls", "count": 3}, status=status
            ),
        )
        restored = _round_trip(event)
        assert restored.WhichOneof("data") == "tool_call"
        assert restored.tool_call.tool == "shell"
        assert dict(restored.tool_call.args) == {"cmd": "ls", "count": 3}
        assert restored.tool_call.status == status
        assert restored == event

    def test_tool_result(self):  # noqa: ANN201  # tracked: #288
        event = messages.make_event(
            ET.EVENT_TYPE_TOOL_RESULT,
            data=events_pb2.ToolResultData(tool="shell", content="out", is_error=True),
        )
        restored = _round_trip(event)
        assert restored.WhichOneof("data") == "tool_result"
        assert restored.tool_result.is_error is True
        assert restored.tool_result.WhichOneof("payload") is None

    def test_tool_result_with_command_payload(self):  # noqa: ANN201  # tracked: #288
        payload = events_pb2.CommandResultPayload(
            stdout="out", stderr="warn", exit_code=2, duration=1.5
        )
        event = messages.make_event(
            ET.EVENT_TYPE_TOOL_RESULT,
            data=events_pb2.ToolResultData(tool="shell", content="out", command=payload),
        )
        restored = _round_trip(event)
        assert restored.tool_result.WhichOneof("payload") == "command"
        assert restored.tool_result.command == payload
        assert restored.tool_result.content == "out"

    def test_tool_result_with_json_payload(self):  # noqa: ANN201  # tracked: #288
        payload = events_pb2.JsonResultPayload()
        payload.value.struct_value.update({"rows": [1, 2], "ok": True})
        event = messages.make_event(
            ET.EVENT_TYPE_TOOL_RESULT,
            data=events_pb2.ToolResultData(
                tool="query", content='{"rows": [1, 2], "ok": true}', json=payload
            ),
        )
        restored = _round_trip(event)
        assert restored.tool_result.WhichOneof("payload") == "json"
        value = restored.tool_result.json.value.struct_value
        assert value["ok"] is True
        assert list(value["rows"]) == [1, 2]

    def test_tool_result_payload_rejects_unknown_kind(self):  # noqa: ANN201  # tracked: #288
        """A payload key outside the oneof is an unknown field on the wire."""
        with pytest.raises(codec.WireError):
            codec.from_dict(
                events_pb2.ToolResultData,
                {"tool": "shell", "content": "out", "mystery": {}},
            )

    def test_tool_result_payload_is_one_of_command_or_json(self):  # noqa: ANN201  # tracked: #288
        """Setting one payload member clears the other (oneof semantics)."""
        data = events_pb2.ToolResultData(tool="t")
        data.command.stdout = "x"
        data.json.value.number_value = 1
        assert data.WhichOneof("payload") == "json"
        assert not data.HasField("command")

    def test_tool_result_without_payload_field_still_validates(self):  # noqa: ANN201  # tracked: #288
        # Old event logs predate the payload field and must keep replaying.
        restored = codec.from_dict(events_pb2.ToolResultData, {"tool": "t", "content": "c"})
        assert restored.WhichOneof("payload") is None

    def test_todo_update(self):  # noqa: ANN201  # tracked: #288
        event = messages.make_event(
            ET.EVENT_TYPE_TODO_UPDATE,
            data=events_pb2.TodoUpdateData(
                todos=[events_pb2.TodoItemData(content="a", status="pending")]
            ),
        )
        restored = _round_trip(event)
        assert restored.WhichOneof("data") == "todo_update"
        assert list(restored.todo_update.todos) == [
            events_pb2.TodoItemData(content="a", status="pending")
        ]

    def test_usage_update(self):  # noqa: ANN201  # tracked: #288
        event = messages.make_event(
            ET.EVENT_TYPE_USAGE_UPDATE,
            data=events_pb2.UsageUpdateData(
                input_tokens=5_000, context_window=1_000_000, model="m"
            ),
        )
        restored = _round_trip(event)
        assert restored.WhichOneof("data") == "usage_update"
        assert restored.usage_update.input_tokens == 5_000

    def test_agent_output_chunk_status_is_optional_and_round_trips(self):  # noqa: ANN201  # tracked: #288
        chunk = events_pb2.AgentOutputChunkData
        assistant = events_pb2.AgentOutputChannel.AGENT_OUTPUT_CHANNEL_ASSISTANT
        bare = messages.make_event(
            ET.EVENT_TYPE_AGENT_OUTPUT_CHUNK,
            data=chunk(channel=assistant, content="hi"),
        )
        restored = _round_trip(bare)
        assert restored.WhichOneof("data") == "agent_output_chunk"
        assert not restored.agent_output_chunk.HasField("status")

        status = events_pb2.AgentStatusData(
            agent_label="Judge", elapsed_seconds=0.5, input_tokens=10
        )
        rich = messages.make_event(
            ET.EVENT_TYPE_AGENT_OUTPUT_CHUNK,
            data=chunk(channel=assistant, content="hi", status=status),
        )
        restored = _round_trip(rich)
        assert restored.agent_output_chunk.status == status


def _sample_events() -> list[events_pb2.RunEvent]:
    """One event per payload kind, each carrying non-default field values."""
    e = events_pb2
    payloads: list[tuple[int, object]] = [
        (ET.EVENT_TYPE_CHAT, e.ChatData(answer="a", thread_title="t", invocation_id="i")),
        (
            ET.EVENT_TYPE_CHAT_THREAD_CREATED,
            e.ChatThreadCreatedData(thread_id="x", title="T", driver="d", provider="p", model="m"),
        ),
        (
            ET.EVENT_TYPE_INVOCATION_STARTED,
            e.InvocationStartedData(system_prompt="s", user_prompt="u"),
        ),
        (ET.EVENT_TYPE_INVOCATION_FINISHED, e.InvocationFinishedData(error="boom")),
        (
            ET.EVENT_TYPE_AGENT_EXECUTION_STARTED,
            e.AgentExecutionStartedData(stage="impl", attempt=2, driver="d"),
        ),
        (
            ET.EVENT_TYPE_AGENT_EXECUTION_ACTIVITY_CHANGED,
            snapshot_pb2.AgentExecutionActivityData(
                mode=snapshot_pb2.ExecutionActivityMode.EXECUTION_ACTIVITY_MODE_TOOL,
                summary="running",
                tool="shell",
            ),
        ),
        (ET.EVENT_TYPE_AGENT_EXECUTION_FINISHED, e.AgentExecutionFinishedData(error="bad")),
        (
            ET.EVENT_TYPE_OUTPUT,
            e.OutputData(stream=e.OutputStream.OUTPUT_STREAM_STDERR, source="s", content="c"),
        ),
        (ET.EVENT_TYPE_SERVER_READY, e.ServerReadyData(socket_protocol="jsonl")),
        (
            ET.EVENT_TYPE_RUN_STARTED,
            e.RunStartedData(outer_loop="l", input="i", max_rounds=3, expected_roles=["a", "b"]),
        ),
        (ET.EVENT_TYPE_RUN_INTERRUPTED, e.RunInterruptedData(reason="r", signal="SIGINT")),
        (
            ET.EVENT_TYPE_RUN_STATUS_CHANGED,
            e.RunStatusChangedData(
                status=common_pb2.RunStatus.RUN_STATUS_PAUSED,
                previous=common_pb2.RunStatus.RUN_STATUS_RUNNING,
            ),
        ),
        (
            ET.EVENT_TYPE_EXPERIMENTS_CHANGED,
            e.ExperimentsChangedData(
                reason=e.ExperimentsChangeReason.EXPERIMENTS_CHANGE_REASON_ROUND_PERSISTED,
                revision=4,
            ),
        ),
        (
            ET.EVENT_TYPE_CONFIGURATION_FAILED,
            e.ConfigurationFailedData(code="c", stage="s", message="m", exit_code=2),
        ),
        (ET.EVENT_TYPE_PHASE_STARTED, e.PhaseData(phase="p", attempt=1)),
        (
            ET.EVENT_TYPE_SUBPROCESS_OUTPUT,
            e.SubprocessOutputData(
                process_id="1",
                process_kind="k",
                stream=e.OutputStream.OUTPUT_STREAM_STDOUT,
                content="c",
            ),
        ),
        (
            ET.EVENT_TYPE_JUDGE_RESULT,
            e.JudgeResultData(verdict=e.JudgeVerdict.JUDGE_VERDICT_PASS, feedback="ok", attempt=1),
        ),
        (ET.EVENT_TYPE_BENCHMARK_RESULT, e.BenchmarkResultData(metric="m", value=1.5, unit="u")),
        (
            ET.EVENT_TYPE_ROUND_FINISHED,
            e.RoundFinishedData(
                attempts=1,
                judge_verdict=e.RoundJudgeVerdict.ROUND_JUDGE_VERDICT_SKIPPED,
                perf_metric=2.0,
                perf_unit="u",
                profile_skipped=True,
            ),
        ),
        (
            ET.EVENT_TYPE_TOOL_CALL,
            e.ToolCallData(tool="t", call_id="c", args={"a": 1}),
        ),
        (
            ET.EVENT_TYPE_TOOL_RESULT,
            e.ToolResultData(tool="t", content="c", command=e.CommandResultPayload(stdout="o")),
        ),
        (
            ET.EVENT_TYPE_TODO_UPDATE,
            e.TodoUpdateData(todos=[e.TodoItemData(content="a", status="pending")]),
        ),
        (ET.EVENT_TYPE_USAGE_UPDATE, e.UsageUpdateData(input_tokens=1, model="m")),
        (
            ET.EVENT_TYPE_AGENT_OUTPUT_CHUNK,
            e.AgentOutputChunkData(
                channel=e.AgentOutputChannel.AGENT_OUTPUT_CHANNEL_TOOL, content="c"
            ),
        ),
        (
            ET.EVENT_TYPE_GATE_STARTED,
            e.GateStartedData(gate=e.GateKind.GATE_KIND_VALIDATION, recipe="r", source=GATES),
        ),
        (
            ET.EVENT_TYPE_GATE_FINISHED,
            e.GateFinishedData(
                gate=e.GateKind.GATE_KIND_BENCHMARK,
                metric="m",
                value=1.0,
                unit="u",
                source=GATES,
            ),
        ),
        (
            ET.EVENT_TYPE_WORKSPACE_SNAPSHOT,
            e.WorkspaceSnapshotData(
                label="l",
                commit="c",
                excluded_paths=["x"],
                source=e.FrameworkSource.FRAMEWORK_SOURCE_GIT_TRACKING,
            ),
        ),
        (
            ET.EVENT_TYPE_RUN_CONFIGURED,
            e.RunConfiguredData(
                run_log_path="/l", project_root="/p", benchmark_contract=True, source=LOOP
            ),
        ),
        (
            ET.EVENT_TYPE_FRAMEWORK_WARNING,
            e.FrameworkWarningData(summary="s", detail="d", source=LOOP),
        ),
    ]
    events = [messages.make_event(kind, "t", data=data) for kind, data in payloads]  # type: ignore[arg-type]
    finished = messages.make_event(ET.EVENT_TYPE_INVOCATION_FINISHED)
    finished.invocation_finished.result.struct_value.update({"k": [1, "x"]})
    events.append(finished)
    return events


def test_every_payload_kind_round_trips_through_canonical_json():  # noqa: ANN201
    events = _sample_events()
    kinds = {event.WhichOneof("data") for event in events}
    oneof_fields = {f.name for f in events_pb2.RunEvent.DESCRIPTOR.oneofs_by_name["data"].fields}
    assert None not in kinds
    assert oneof_fields <= kinds
    for event in events:
        assert _round_trip(event) == event


def test_round_trip_preserves_envelope_fields():  # noqa: ANN201
    event = messages.make_event(
        ET.EVENT_TYPE_CHAT,
        "hello",
        data=events_pb2.ChatData(answer="a"),
        status=ES.EVENT_STATUS_ANSWERED,
        round_label="r1",
        agent_kind="impl",
        execution_id="ex",
        chat_thread_id="th",
    )
    event.sequence = 9
    event.run_id = "run"
    assert _round_trip(event) == event


class TestBackwardCompatibility:
    """Hand-written version 1 records double as legacy-compat coverage."""

    def test_v1_chunk_without_status_field_still_parses(self):  # noqa: ANN201  # tracked: #288
        """Events recorded by older backends omit the new optional fields."""
        raw = (
            '{"protocol_version": 1, "sequence": 3, "run_id": "r", '
            '"timestamp": "2026-01-01T00:00:00Z", "type": "agent_output_chunk", '
            '"data": {"kind": "agent_output_chunk", "channel": "tool", "content": "x"}}'
        )
        event = parse_event(raw)
        assert event.WhichOneof("data") == "agent_output_chunk"
        assert event.type == ET.EVENT_TYPE_AGENT_OUTPUT_CHUNK
        assert event.agent_output_chunk.channel == (
            events_pb2.AgentOutputChannel.AGENT_OUTPUT_CHANNEL_TOOL
        )
        assert not event.agent_output_chunk.HasField("status")
        assert event.protocol_version == 2

    def test_v1_tool_result_payload_upgrades(self):  # noqa: ANN201
        raw = json.dumps(
            {
                "protocol_version": 1,
                "sequence": 1,
                "run_id": "r",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "tool_result",
                "data": {
                    "kind": "tool_result",
                    "tool": "sh",
                    "content": "o",
                    "payload": {"kind": "command", "stdout": "o", "stderr": "", "exit_code": 0},
                },
            }
        )
        event = parse_event(raw)
        assert event.tool_result.WhichOneof("payload") == "command"
        assert event.tool_result.command.stdout == "o"

    def test_v1_invocation_id_folds_into_execution_id(self):  # noqa: ANN201
        raw = (
            '{"protocol_version": 1, "sequence": 1, "run_id": "r", '
            '"timestamp": "2026-01-01T00:00:00Z", "type": "output", '
            '"invocation_id": "inv-1"}'
        )
        event = parse_event(raw)
        assert event.execution_id == "inv-1"

    def test_unknown_data_kind_rejected(self):  # noqa: ANN201  # tracked: #288
        raw = (
            '{"protocol_version": 1, "timestamp": "2026-01-01T00:00:00Z", '
            '"type": "output", "data": {"kind": "not_a_kind"}}'
        )
        with pytest.raises(codec.WireError):
            parse_event(raw)

    def test_unknown_v2_field_rejected(self):  # noqa: ANN201
        """Pydantic extra=forbid becomes strict parsing of unknown proto fields."""
        with pytest.raises(codec.WireError):
            codec.from_dict(
                events_pb2.RunEvent,
                {
                    "protocol_version": 2,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "EVENT_TYPE_OUTPUT",
                    "surprise": 1,
                },
            )

    def test_unspecified_type_rejected(self):  # noqa: ANN201
        raw = '{"protocol_version": 2, "timestamp": "2026-01-01T00:00:00Z"}'
        with pytest.raises(codec.WireError):
            parse_event(raw)

    def test_messages_replace_derives_variant_without_mutating_original(self):  # noqa: ANN201
        """Replaces the Pydantic model_copy / frozen behavior."""
        event = messages.make_event(ET.EVENT_TYPE_OUTPUT, "x", round_label="r1")
        variant = messages.replace(event, sequence=5, round_label=None)
        assert variant.sequence == 5
        assert not variant.HasField("round_label")
        assert event.sequence == 0
        assert event.round_label == "r1"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_benchmark_result_rejects_non_finite_value(value):  # noqa: ANN001, ANN201  # tracked: #288
    event = messages.make_event(
        ET.EVENT_TYPE_BENCHMARK_RESULT,
        data=events_pb2.BenchmarkResultData(metric="throughput", value=value, unit="req/s"),
    )
    with pytest.raises(codec.WireError, match="finite number"):
        validate.validate_event(event)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_round_finished_rejects_non_finite_perf_metric(value):  # noqa: ANN001, ANN201  # tracked: #288
    event = messages.make_event(
        ET.EVENT_TYPE_ROUND_FINISHED,
        data=events_pb2.RoundFinishedData(
            attempts=1,
            judge_verdict=events_pb2.RoundJudgeVerdict.ROUND_JUDGE_VERDICT_PASS,
            perf_metric=value,
            perf_unit="req/s",
        ),
    )
    with pytest.raises(codec.WireError, match="finite number"):
        validate.validate_event(event)


class TestRoundFinishedProfileSkipped:
    def test_flag_round_trips_when_true(self):  # noqa: ANN201  # tracked: #288
        event = messages.make_event(
            ET.EVENT_TYPE_ROUND_FINISHED,
            data=events_pb2.RoundFinishedData(
                attempts=1,
                judge_verdict=events_pb2.RoundJudgeVerdict.ROUND_JUDGE_VERDICT_PASS,
                perf_metric=100.0,
                perf_unit="req/s",
                profile_skipped=True,
            ),
        )
        restored = _round_trip(event)
        assert restored.round_finished.profile_skipped is True

    def test_v1_payload_without_flag_defaults_false(self):  # noqa: ANN201  # tracked: #288
        """Events recorded before the field existed must keep replaying."""
        raw = (
            '{"protocol_version": 1, "sequence": 7, "run_id": "r", '
            '"timestamp": "2026-01-01T00:00:00Z", "type": "round_finished", '
            '"data": {"kind": "round_finished", "attempts": 2, "judge_verdict": "pass", '
            '"perf_metric": 100.0, "perf_unit": "req/s"}}'
        )
        event = parse_event(raw)
        assert event.WhichOneof("data") == "round_finished"
        assert event.round_finished.profile_skipped is False
        assert event.round_finished.attempts == 2


class TestFrameworkEventRoundTrip:
    def test_gate_started(self):  # noqa: ANN201
        event = messages.make_event(
            ET.EVENT_TYPE_GATE_STARTED,
            status=ES.EVENT_STATUS_ACTIVE,
            round_label="round-3",
            data=events_pb2.GateStartedData(
                gate=events_pb2.GateKind.GATE_KIND_VALIDATION,
                recipe="focused-tests",
                command="uv run pytest -q",
                source=GATES,
            ),
        )
        restored = _round_trip(event)
        data = restored.gate_started
        assert data.gate == events_pb2.GateKind.GATE_KIND_VALIDATION
        assert data.recipe == "focused-tests"
        assert data.command == "uv run pytest -q"
        assert data.source == GATES
        assert restored.status == ES.EVENT_STATUS_ACTIVE
        assert restored.round_label == "round-3"

    def test_gate_finished_pass_with_metric(self):  # noqa: ANN201
        event = messages.make_event(
            ET.EVENT_TYPE_GATE_FINISHED,
            status=ES.EVENT_STATUS_COMPLETED,
            data=events_pb2.GateFinishedData(
                gate=events_pb2.GateKind.GATE_KIND_BENCHMARK,
                metric="tok_per_sec",
                value=42.5,
                unit="tok/s",
                source=GATES,
            ),
        )
        restored = _round_trip(event)
        data = restored.gate_finished
        assert data.gate == events_pb2.GateKind.GATE_KIND_BENCHMARK
        assert data.metric == "tok_per_sec"
        assert data.value == 42.5
        assert data.unit == "tok/s"
        assert data.reused is False
        assert restored.status == ES.EVENT_STATUS_COMPLETED

    def test_gate_finished_failure_with_output_tail(self):  # noqa: ANN201
        event = messages.make_event(
            ET.EVENT_TYPE_GATE_FINISHED,
            status=ES.EVENT_STATUS_FAILED,
            data=events_pb2.GateFinishedData(
                gate=events_pb2.GateKind.GATE_KIND_ACCURACY,
                output_tail="assertion mismatch",
                source=GATES,
            ),
        )
        restored = _round_trip(event)
        assert restored.gate_finished.output_tail == "assertion mismatch"
        assert restored.status == ES.EVENT_STATUS_FAILED
        # A failed gate is an expected outcome, not a run fault: no diagnostic.
        assert not restored.HasField("diagnostic")

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_gate_finished_rejects_non_finite_value(self, value):  # noqa: ANN001, ANN201
        event = messages.make_event(
            ET.EVENT_TYPE_GATE_FINISHED,
            data=events_pb2.GateFinishedData(
                gate=events_pb2.GateKind.GATE_KIND_BENCHMARK,
                metric="m",
                value=value,
                source=GATES,
            ),
        )
        with pytest.raises(codec.WireError, match="finite"):
            validate.validate_event(event)

    def test_workspace_snapshot(self):  # noqa: ANN201
        source = events_pb2.FrameworkSource.FRAMEWORK_SOURCE_GIT_TRACKING
        event = messages.make_event(
            ET.EVENT_TYPE_WORKSPACE_SNAPSHOT,
            data=events_pb2.WorkspaceSnapshotData(label="round-2", commit="a" * 40, source=source),
        )
        restored = _round_trip(event)
        data = restored.workspace_snapshot
        assert data.label == "round-2"
        assert data.commit == "a" * 40
        assert not data.HasField("baseline")
        assert list(data.excluded_paths) == []
        assert data.source == source

    def test_run_configured(self):  # noqa: ANN201
        event = messages.make_event(
            ET.EVENT_TYPE_RUN_CONFIGURED,
            data=events_pb2.RunConfiguredData(
                run_log_path="/logs/run-1.log",
                project_root="/work/project",
                model="claude-sonnet-4-6",
                objective="Make the queue fast.",
                search_policy="pareto-ucb",
                benchmark_contract=True,
                pareto_objectives="[latency(min)], frontier_bias=0.5",
                source=LOOP,
            ),
        )
        restored = _round_trip(event)
        data = restored.run_configured
        assert data.run_log_path == "/logs/run-1.log"
        assert data.search_policy == "pareto-ucb"
        assert data.benchmark_contract is True

    def test_framework_warning_with_diagnostic(self):  # noqa: ANN201
        """The projection lifts the payload into the wire diagnostic field."""
        event = messages.make_event(
            ET.EVENT_TYPE_FRAMEWORK_WARNING,
            data=events_pb2.FrameworkWarningData(
                summary="profiler failed", detail="boom", source=LOOP
            ),
            diagnostic=make_diagnostic(
                code="framework_warning",
                summary="profiler failed",
                detail="boom",
                scope=DiagnosticScope.DIAGNOSTIC_SCOPE_RUN,
                severity=DiagnosticSeverity.DIAGNOSTIC_SEVERITY_WARNING,
                source="loop",
            ),
        )
        restored = _round_trip(event)
        assert restored.framework_warning.summary == "profiler failed"
        assert restored.framework_warning.source == LOOP
        assert restored.HasField("diagnostic")
        assert restored.diagnostic.severity == DiagnosticSeverity.DIAGNOSTIC_SEVERITY_WARNING
        assert restored.diagnostic.source == "loop"

    def test_diagnostic_without_source_still_parses(self):  # noqa: ANN201
        """`source` is additive: persisted diagnostics without it stay valid."""
        raw = (
            '{"code": "framework_warning", "summary": "s",'
            ' "scope": "DIAGNOSTIC_SCOPE_RUN", "severity": "DIAGNOSTIC_SEVERITY_WARNING"}'
        )
        diagnostic = codec.loads(common_pb2.Diagnostic, raw)
        assert not diagnostic.HasField("source")
