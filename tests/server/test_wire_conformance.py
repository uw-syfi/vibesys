"""Conformance of the protobuf wire form.

The golden corpus under ``clients/backend-client/testdata`` is the shared
contract between the Python server and the TypeScript client: this module and
``clients/backend-client/src/conformance.test.ts`` both parse every line and
require the re-serialized JSON to equal the recorded object. The event lines
are selected from the real version 1 fixtures under ``clients/tui/dev/fixtures``
and upgraded, so the corpus also pins the version 1 upgrade.

Regenerate the corpus after an intentional wire change with
``VIBESYS_UPDATE_WIRE_GOLDEN=1 uv run pytest tests/server/test_wire_conformance.py``.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from server.wire import PROTOCOL_VERSION, codec, messages, upgrade, validate
from server.wire.codec import WireError
from server.wire.v2 import (
    common_pb2,
    events_pb2,
    requests_pb2,
    responses_pb2,
    server_messages_pb2,
    snapshot_pb2,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "clients" / "tui" / "dev" / "fixtures"
GOLDEN = REPO / "clients" / "backend-client" / "testdata"
EVENTS_GOLDEN = GOLDEN / "events.v2.jsonl"
MESSAGES_GOLDEN = GOLDEN / "messages.v2.jsonl"

_PAYLOAD_CASES = {
    field.name for field in events_pb2.RunEvent.DESCRIPTOR.oneofs_by_name["data"].fields
}

MESSAGE_TYPES = {
    "Request": requests_pb2.Request,
    "Response": responses_pb2.Response,
    "ServerMessage": server_messages_pb2.ServerMessage,
    "RunSnapshot": snapshot_pb2.RunSnapshot,
}


def _fixture_records() -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(FIXTURES.glob("*.jsonl")):
        records.extend(
            (path.name, json.loads(line)) for line in path.read_text().splitlines() if line.strip()
        )
    return records


def _synthetic_v1_records() -> list[dict[str, Any]]:
    """Version 1 records for payloads no bundled fixture exercises."""
    base = {
        "protocol_version": 1,
        "run_id": "syn",
        "timestamp": "2026-09-01T12:00:00.5Z",
        "text": "",
    }
    payloads: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("run_status_changed", {"status": "running", "previous": "starting"}, {}),
        (
            "chat_thread_created",
            {
                "thread_id": "t1",
                "title": "T",
                "driver": "agentshim",
                "provider": "claude",
                "model": "m",
                "created_at": "2026-09-01T12:00:00Z",
            },
            {"chat_thread_id": "t1"},
        ),
        (
            "configuration_failed",
            {"code": "bad_flag", "stage": "cli", "message": "no", "usage": None, "exit_code": 2},
            {},
        ),
        ("output", {"stream": "stderr", "source": "backend", "content": "warn"}, {}),
        ("todo_update", {"todos": [{"content": "a", "status": "in_progress"}]}, {}),
        (
            "tool_result",
            {
                "tool": "read",
                "call_id": "c1",
                "content": "{}",
                "is_error": False,
                "payload": {"kind": "json", "value": {"a": [1, 2.5, None, "x"]}},
            },
            {},
        ),
    ]
    records = []
    for sequence, (kind, body, envelope) in enumerate(payloads, start=1):
        records.append(
            {
                **base,
                "sequence": sequence,
                "type": kind,
                "status": None,
                "data": {"kind": kind, **body},
                **envelope,
            }
        )
    return records


def _selected_upgraded_events() -> list[dict[str, Any]]:
    """First real event per fixture, event type, and payload kind, upgraded to v2."""
    seen: set[tuple[str, str, str | None, str | None]] = set()
    selected: list[dict[str, Any]] = []
    for name, record in _fixture_records():
        data = record.get("data") or {}
        key = (name, record["type"], data.get("kind"), record.get("status"))
        if key in seen:
            continue
        seen.add(key)
        selected.append(codec.to_dict(upgrade.event_from_legacy(record)))
    selected.extend(codec.to_dict(upgrade.event_from_legacy(r)) for r in _synthetic_v1_records())
    return selected


def _sample_messages() -> list[dict[str, Any]]:
    """One representative message per request body, response section, and server message."""
    stamp = Timestamp()
    stamp.FromJsonString("2026-09-01T12:00:00.250Z")
    requests = [
        ("pause", {}),
        ("resume", {}),
        ("steer", {"text": "try a smaller batch"}),
        ("stop", {}),
        ("snapshot", {}),
        ("chat", {"text": "why did round 2 fail?", "thread_id": "t1"}),
        ("chat_thread_create", {"provider": "claude", "model": "m", "title": "T"}),
        ("chat_options", {}),
        ("tui_defaults", {}),
        ("history", {}),
        ("performance", {}),
        (
            "experiments",
            {"after": requests_pb2.ExperimentCursor(run_id="r", projection_id="p", revision=3)},
        ),
        ("design", {}),
        ("design_patch", {"base": "a1", "head": "b2", "path": "src/x.py"}),
        ("events", {"after_sequence": 5, "before_sequence": 9, "timeout_ms": 250}),
        ("subscribe", {"after_sequence": 7, "tail": 100, "store_id": "s"}),
    ]
    out: list[dict[str, Any]] = []
    for body, fields in requests:
        request = messages.make_request(body, **fields)
        request.request_id = f"req-{body}"
        request.timestamp.CopyFrom(stamp)
        out.append({"message": "Request", "json": codec.to_dict(request)})
    out.extend(_sample_responses(stamp))
    return out


def _sample_responses(stamp: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    diagnostic = common_pb2.Diagnostic(
        id="d1",
        code="invalid_value",
        summary="Request received invalid input",
        scope=common_pb2.DIAGNOSTIC_SCOPE_REQUEST,
        severity=common_pb2.DIAGNOSTIC_SEVERITY_ERROR,
        retryability=common_pb2.DIAGNOSTIC_RETRYABILITY_UNKNOWN,
        hint="check the arguments",
    )
    failed = responses_pb2.Response(
        protocol_version=PROTOCOL_VERSION,
        request_id="req-1",
        timestamp=stamp,
        ok=False,
        error="Request received invalid input",
        diagnostic=diagnostic,
    )
    entry = responses_pb2.HypothesisEntry(
        hypothesis_id="h1",
        identified=True,
        title="Batch decode",
        first_round=1,
        last_round=2,
        judge_verdict=events_pb2.JUDGE_VERDICT_PASS,
        perf_metric=12.5,
        perf_direction=responses_pb2.OBJECTIVE_DIRECTION_MAX,
        kept=True,
        rounds=[
            responses_pb2.HypothesisRound(
                round=1,
                passed=True,
                reviewed=True,
                hypothesis_outcome=responses_pb2.HYPOTHESIS_OUTCOME_SUPPORTED,
                judge_verdict=responses_pb2.ROUND_REVIEW_VERDICT_DEFERRED,
                perf_metric=12.5,
                candidate_disposition=responses_pb2.CANDIDATE_DISPOSITION_PARETO_FRONTIER,
            )
        ],
    )
    sections = responses_pb2.Response(
        protocol_version=PROTOCOL_VERSION,
        request_id="req-2",
        timestamp=stamp,
        ok=True,
        ack=responses_pb2.CommandAck(
            action=responses_pb2.COMMAND_ACTION_PAUSE,
            status=responses_pb2.COMMAND_ACK_STATUS_PENDING,
        ),
        experiments=[entry],
        experiments_ready=True,
        experiment_update=responses_pb2.ExperimentUpdate(
            run_id="r", projection_id="p", through_revision=4, reset=True
        ),
        performance=[
            responses_pb2.PerformanceRound(round=1, perf_metric=1.5, perf_unit="tok/s", passed=True)
        ],
        design=[
            responses_pb2.DesignRound(
                round=1,
                commit="c1",
                base="c0",
                files=responses_pb2.DesignFiles(
                    changes=[
                        responses_pb2.DesignFileChange(
                            path="a.py",
                            change=responses_pb2.DESIGN_CHANGE_RENAMED,
                            renamed_from="b.py",
                        )
                    ]
                ),
            )
        ],
        chat_options=responses_pb2.ChatOptions(
            providers=[
                responses_pb2.ChatProviderOptions(
                    provider="claude",
                    models=[
                        responses_pb2.ChatModelOption(
                            model="m", source=responses_pb2.CHAT_MODEL_SOURCE_RUN, default=True
                        )
                    ],
                )
            ]
        ),
        tui_defaults=responses_pb2.TuiDefaults(
            runs_dir="runs",
            input_path="in.md",
            experiment_name="exp",
            repository_name="repo",
            visibility=responses_pb2.REPOSITORY_VISIBILITY_PRIVATE,
            theme=responses_pb2.TUI_THEME_SOLARIZED_DARK,
        ),
    )
    snapshot = snapshot_pb2.RunSnapshot(
        protocol_version=PROTOCOL_VERSION,
        run_id="r",
        sequence=10,
        status=common_pb2.RUN_STATUS_PAUSING,
        chat_threads=[
            snapshot_pb2.ChatThreadInfo(
                thread_id="t", driver="agentshim", provider="claude", model="m"
            )
        ],
    )
    event = messages.make_event(
        events_pb2.EVENT_TYPE_CHAT, "hello", data=events_pb2.ChatData(answer="hi")
    )
    event.sequence = 3
    event.timestamp.CopyFrom(stamp)
    batch = server_messages_pb2.ServerMessage(
        event_batch=server_messages_pb2.EventBatchMessage(
            events=[event], through_sequence=3, store_id="s", history_after_sequence=0
        )
    )
    others = [
        server_messages_pb2.ServerMessage(
            subscribed=server_messages_pb2.SubscribedMessage(
                request_id="r", run_id="run", latest_sequence=3
            )
        ),
        server_messages_pb2.ServerMessage(event=event),
        batch,
        server_messages_pb2.ServerMessage(
            protocol_error=server_messages_pb2.ProtocolErrorMessage(
                code="protocol_version_unsupported", message="upgrade", diagnostic=diagnostic
            )
        ),
    ]
    return [
        {"message": "Response", "json": codec.to_dict(failed)},
        {"message": "Response", "json": codec.to_dict(sections)},
        {"message": "RunSnapshot", "json": codec.to_dict(snapshot)},
        *({"message": "ServerMessage", "json": codec.to_dict(item)} for item in others),
    ]


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n" for record in records
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module", autouse=True)
def _maybe_update_golden() -> None:
    if os.environ.get("VIBESYS_UPDATE_WIRE_GOLDEN"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        EVENTS_GOLDEN.write_text(_jsonl(_selected_upgraded_events()))
        MESSAGES_GOLDEN.write_text(_jsonl(_sample_messages()))


def test_golden_events_are_the_upgraded_real_fixtures() -> None:
    assert _read_jsonl(EVENTS_GOLDEN) == json.loads(json.dumps(_selected_upgraded_events()))


def test_golden_messages_are_current() -> None:
    assert _read_jsonl(MESSAGES_GOLDEN) == json.loads(json.dumps(_sample_messages()))


def test_golden_events_round_trip_and_validate() -> None:
    records = _read_jsonl(EVENTS_GOLDEN)
    assert records
    for record in records:
        event = codec.from_dict(events_pb2.RunEvent, record)
        validate.validate_event(event)
        assert codec.to_dict(event) == record
        assert codec.from_dict(events_pb2.RunEvent, json.loads(codec.dumps(event))) == event


def test_golden_messages_round_trip() -> None:
    for record in _read_jsonl(MESSAGES_GOLDEN):
        cls = MESSAGE_TYPES[record["message"]]
        message = codec.from_dict(cls, record["json"])
        assert codec.to_dict(message) == record["json"]
        if isinstance(message, requests_pb2.Request):
            validate.validate_request(message)


def test_corpus_covers_every_event_payload_case() -> None:
    covered = {
        key for record in _read_jsonl(EVENTS_GOLDEN) for key in record if key in _PAYLOAD_CASES
    }
    assert covered == _PAYLOAD_CASES


def test_every_v1_fixture_line_upgrades_and_validates() -> None:
    lines = 0
    for _, record in _fixture_records():
        event = upgrade.event_from_legacy(record)
        validate.validate_event(event)
        assert event.protocol_version == PROTOCOL_VERSION
        legacy_id = record.get("execution_id") or record.get("invocation_id")
        assert (event.execution_id if event.HasField("execution_id") else None) == legacy_id
        assert event.sequence == record["sequence"]
        assert upgrade.is_legacy(record)
        lines += 1
    assert lines > 1000


def test_upgrade_maps_legacy_payloads_to_the_typed_oneof() -> None:
    record = {
        "protocol_version": 1,
        "sequence": 4,
        "run_id": "r",
        "timestamp": "2026-09-01T12:00:00Z",
        "type": "tool_result",
        "text": "",
        "status": "completed",
        "invocation_id": "e1",
        "data": {
            "kind": "tool_result",
            "tool": "bash",
            "call_id": None,
            "content": "ok",
            "is_error": False,
            "payload": {
                "kind": "command",
                "stdout": "ok",
                "stderr": "",
                "exit_code": 0,
                "duration": 1.5,
            },
        },
    }
    event = upgrade.event_from_legacy(record)
    assert event.execution_id == "e1"
    assert event.type == events_pb2.EVENT_TYPE_TOOL_RESULT
    assert event.status == events_pb2.EVENT_STATUS_COMPLETED
    assert event.WhichOneof("data") == "tool_result"
    assert event.tool_result.WhichOneof("payload") == "command"
    assert event.tool_result.command.duration == 1.5
    assert "invocation_id" not in codec.to_dict(event)


def _request(body: dict[str, Any], **envelope: Any) -> str:  # noqa: ANN401
    return json.dumps({"protocol_version": PROTOCOL_VERSION, "request_id": "x", **envelope, **body})


def test_parse_request_accepts_a_valid_request_and_fills_defaults() -> None:
    request = codec.parse_request(json.dumps({"protocol_version": 2, "snapshot": {}}))
    assert request.WhichOneof("body") == "snapshot"
    assert request.request_id
    assert request.HasField("timestamp")


@pytest.mark.parametrize(
    "line",
    [
        _request({"snapshot": {}, "surprise": 1}),
        _request({"subscribe": {"after_sequence": 0, "surprise": 1}}),
        _request({"chat_thread_create": {"driver": "CHAT_DRIVER_NOPE"}}),
        _request({"snapshot": {}, "history": {}}),
        _request({"events": {"after_sequence": -1}}),
        "not json",
        "[]",
    ],
)
def test_parse_request_rejects_unknown_or_malformed_input(line: str) -> None:
    with pytest.raises(WireError) as raised:
        codec.parse_request(line)
    assert raised.value.code in {"invalid_message", "invalid_json"}


def test_unknown_field_rejection_is_the_capability_probe() -> None:
    """A server predating ``tail`` rejects it; this server rejects a future field the same way."""
    with pytest.raises(WireError, match="future_field") as raised:
        codec.parse_request(_request({"subscribe": {"after_sequence": 0, "future_field": 1}}))
    assert raised.value.code == "invalid_message"


@pytest.mark.parametrize(
    "line",
    [
        json.dumps({"protocol_version": 1, "type": "query.snapshot", "request_id": "x"}),
        json.dumps({"type": "subscribe", "after_sequence": 0}),
        json.dumps({"protocol_version": 1, "snapshot": {}}),
    ],
)
def test_version_1_clients_are_rejected_clearly(line: str) -> None:
    with pytest.raises(WireError, match="protocol version 2") as raised:
        codec.parse_request(line)
    assert raised.value.code == "protocol_version_unsupported"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({}, "exactly one body"),
        ({"steer": {"text": ""}}, "steer.text"),
        ({"steer": {}}, "steer.text"),
        ({"events": {"timeout_ms": 30_001}}, "timeout_ms"),
        ({"events": {"before_sequence": 0}}, "before_sequence"),
        ({"subscribe": {"tail": 0}}, "tail"),
        ({"chat_thread_create": {"driver": "CHAT_DRIVER_UNSPECIFIED"}}, "driver"),
    ],
)
def test_validation_that_pydantic_used_to_enforce(body: dict[str, Any], fragment: str) -> None:
    with pytest.raises(WireError, match=fragment) as raised:
        codec.parse_request(_request(body))
    assert raised.value.code == "invalid_message"


def test_events_timeout_boundary_is_accepted() -> None:
    codec.parse_request(_request({"events": {"timeout_ms": 30_000, "before_sequence": 1}}))
    codec.parse_request(_request({"subscribe": {"tail": 1}}))


def _event(**kwargs: Any) -> events_pb2.RunEvent:  # noqa: ANN401
    return messages.make_event(events_pb2.EVENT_TYPE_BENCHMARK_RESULT, **kwargs)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_doubles_are_rejected(value: float) -> None:
    event = _event(data=events_pb2.BenchmarkResultData(metric="m", value=value, unit="u"))
    with pytest.raises(WireError, match="finite"):
        validate.validate_event(event)


def test_non_finite_double_in_json_form_is_rejected() -> None:
    record = codec.to_dict(
        _event(data=events_pb2.BenchmarkResultData(metric="m", value=1.0, unit="u"))
    )
    record["benchmark_result"]["value"] = "NaN"
    with pytest.raises(WireError, match="finite"):
        validate.validate_event(codec.from_dict(events_pb2.RunEvent, record))


def test_response_with_a_non_finite_double_is_rejected() -> None:
    response = responses_pb2.Response(
        performance=[responses_pb2.PerformanceRound(round=1, perf_metric=math.nan, perf_unit="u")]
    )
    with pytest.raises(WireError, match="performance"):
        validate.validate_message(response)


def test_unspecified_enums_and_missing_timestamp_are_rejected() -> None:
    event = _event(data=events_pb2.BenchmarkResultData(metric="m", value=1.0, unit="u"))
    event.type = events_pb2.EVENT_TYPE_UNSPECIFIED
    with pytest.raises(WireError, match="type"):
        validate.validate_event(event)
    bare = events_pb2.RunEvent(protocol_version=PROTOCOL_VERSION, type=events_pb2.EVENT_TYPE_CHAT)
    with pytest.raises(WireError, match="timestamp"):
        validate.validate_event(bare)


def test_event_with_a_wrong_protocol_version_is_rejected() -> None:
    event = _event(data=events_pb2.BenchmarkResultData(metric="m", value=1.0, unit="u"))
    event.protocol_version = 1
    with pytest.raises(WireError) as raised:
        validate.validate_event(event)
    assert raised.value.code == "protocol_version_unsupported"


def test_event_json_rejects_unknown_fields_and_enum_names() -> None:
    record = codec.to_dict(
        _event(data=events_pb2.BenchmarkResultData(metric="m", value=1.0, unit="u"))
    )
    with pytest.raises(WireError):
        codec.from_dict(events_pb2.RunEvent, {**record, "kind": "benchmark_result"})
    with pytest.raises(WireError):
        codec.from_dict(events_pb2.RunEvent, {**record, "type": "benchmark_result"})
