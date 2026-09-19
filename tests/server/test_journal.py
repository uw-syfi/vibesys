"""Durable journal attachment, replay translation, diagnostics, and failure-helper tests."""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING

import pytest
from tests.server.support import build_server_parts

if TYPE_CHECKING:
    from pathlib import Path

from google.protobuf import struct_pb2

from server.diagnostics import (
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
)
from server.wire import codec, messages
from server.wire.v2 import events_pb2, snapshot_pb2

EventType = events_pb2.EventType
EventStatus = events_pb2.EventStatus


def _ok_struct():  # noqa: ANN202
    struct = struct_pb2.Struct()
    struct.update({"ok": True})
    return struct


def _events(path):  # noqa: ANN001, ANN202
    return [json.loads(line) for line in path.read_text().splitlines()]


def _write_stored_events(log_dir, events):  # noqa: ANN001, ANN202
    log_dir.mkdir()
    (log_dir / "run-events.jsonl").write_text(
        "".join(codec.dumps(event) + "\n" for event in events)
    )


def _stored_event(sequence, event_type, status, data):  # noqa: ANN001, ANN202
    return messages.make_event(
        event_type,
        data=data,
        sequence=sequence,
        run_id="persisted-run",
        status=status,
        agent_kind="implementer",
        round_label="round 1",
        execution_id="exec-1",
    )


def test_bootstrap_events_join_durable_history(tmp_path):  # noqa: ANN001, ANN201
    durable = build_server_parts(tmp_path / "durable")
    durable.journal.record(
        EventType.EVENT_TYPE_RUN_FINISHED, status=EventStatus.EVENT_STATUS_COMPLETED
    )

    current = build_server_parts(tmp_path / "bootstrap")
    current.journal.record(
        EventType.EVENT_TYPE_SERVER_READY, status=EventStatus.EVENT_STATUS_ACTIVE
    )
    current.attach(tmp_path / "durable")
    current.journal.record(EventType.EVENT_TYPE_RUN_STARTED, status=EventStatus.EVENT_STATUS_ACTIVE)

    # Each attach publishes the starting -> running transition, and the second
    # session's lands after the first session's terminal event: a client that
    # folds this history in order ends at `running`, not at the old `completed`.
    expected = [
        EventType.EVENT_TYPE_SERVER_STARTED,
        EventType.EVENT_TYPE_RUN_STATUS_CHANGED,
        EventType.EVENT_TYPE_RUN_FINISHED,
        EventType.EVENT_TYPE_SERVER_STARTED,
        EventType.EVENT_TYPE_RUN_STATUS_CHANGED,
        EventType.EVENT_TYPE_SERVER_READY,
        EventType.EVENT_TYPE_RUN_STARTED,
    ]
    assert [event.type for event in current.journal.read()] == expected
    assert [event["type"] for event in _events(tmp_path / "durable/run-events.jsonl")] == [
        EventType.Name(event_type) for event_type in expected
    ]


def test_attach_renumbering_bootstrap_events_starts_a_new_sequence_space(tmp_path):  # noqa: ANN001, ANN201
    durable = build_server_parts(tmp_path / "durable")
    durable.journal.record(
        EventType.EVENT_TYPE_RUN_FINISHED, status=EventStatus.EVENT_STATUS_COMPLETED
    )

    current = build_server_parts(tmp_path / "bootstrap")
    bootstrap_store = current.journal.store_id_locked()
    folded = current.journal.read()
    current.attach(tmp_path / "durable")
    attached = current.journal.read()[: len(folded)]

    # The bootstrap events were re-appended onto the durable log's tail, so the
    # sequences a subscriber already folded now name different events. That is
    # a different sequence space and the identity has to say so.
    assert current.journal.store_id_locked() != bootstrap_store
    assert [event.sequence for event in attached] == [event.sequence for event in folded]
    assert attached != folded


def test_attach_into_an_empty_log_keeps_the_sequence_space(tmp_path):  # noqa: ANN001, ANN201
    current = build_server_parts(tmp_path / "bootstrap")
    bootstrap_store = current.journal.store_id_locked()
    before = [(event.sequence, event.type) for event in current.journal.read()]

    current.attach(tmp_path / "durable")

    # Nothing was renumbered, so every folded sequence still names the same
    # event and a subscriber has nothing to re-fold.
    assert current.journal.store_id_locked() == bootstrap_store
    assert [(event.sequence, event.type) for event in current.journal.read()] == before


def test_phase_only_legacy_replay_translates_in_place(tmp_path):  # noqa: ANN001, ANN201
    """A translated phase event replaces the original at its stored sequence."""
    log_dir = tmp_path / "legacy"
    _write_stored_events(
        log_dir,
        [
            _stored_event(
                1,
                EventType.EVENT_TYPE_PHASE_STARTED,
                EventStatus.EVENT_STATUS_ACTIVE,
                events_pb2.PhaseData(phase="implementer", attempt=1),
            ),
            _stored_event(
                2,
                EventType.EVENT_TYPE_PHASE_FINISHED,
                EventStatus.EVENT_STATUS_COMPLETED,
                events_pb2.PhaseData(phase="implementer", attempt=1),
            ),
        ],
    )

    parts = build_server_parts(log_dir)
    events = parts.journal.read()

    sequences = [event.sequence for event in events]
    assert all(left < right for left, right in itertools.pairwise(sequences)), sequences
    started, finished = events[0], events[1]
    assert started.type == EventType.EVENT_TYPE_AGENT_EXECUTION_STARTED
    assert started.sequence == 1
    assert started.WhichOneof("data") == "agent_execution_started"
    assert started.agent_execution_started.stage == "implementer"
    assert started.agent_execution_started.attempt == 1
    assert finished.type == EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED
    assert finished.sequence == 2
    assert finished.status == EventStatus.EVENT_STATUS_COMPLETED
    assert not any(
        event.type in {EventType.EVENT_TYPE_PHASE_STARTED, EventType.EVENT_TYPE_PHASE_FINISHED}
        for event in events
    )


def test_modern_phase_events_replay_unchanged(tmp_path):  # noqa: ANN001, ANN201
    """Phase events with a canonical lifecycle sibling pass through untouched."""
    log_dir = tmp_path / "modern"
    activity = snapshot_pb2.AgentExecutionActivityData(
        mode=snapshot_pb2.ExecutionActivityMode.EXECUTION_ACTIVITY_MODE_THINKING,
        summary="Implementing",
    )
    stored = [
        _stored_event(
            1,
            EventType.EVENT_TYPE_AGENT_EXECUTION_STARTED,
            EventStatus.EVENT_STATUS_ACTIVE,
            events_pb2.AgentExecutionStartedData(stage="implementer", attempt=1, activity=activity),
        ),
        _stored_event(
            2,
            EventType.EVENT_TYPE_PHASE_STARTED,
            EventStatus.EVENT_STATUS_ACTIVE,
            events_pb2.PhaseData(phase="implementer", attempt=1),
        ),
        _stored_event(
            3,
            EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
            EventStatus.EVENT_STATUS_COMPLETED,
            events_pb2.AgentExecutionFinishedData(
                result=struct_pb2.Value(struct_value=_ok_struct())
            ),
        ),
        _stored_event(
            4,
            EventType.EVENT_TYPE_PHASE_FINISHED,
            EventStatus.EVENT_STATUS_COMPLETED,
            events_pb2.PhaseData(phase="implementer", attempt=1),
        ),
    ]
    _write_stored_events(log_dir, stored)

    parts = build_server_parts(log_dir)
    events = parts.journal.read()

    assert [codec.dumps(event) for event in events[: len(stored)]] == [
        codec.dumps(event) for event in stored
    ]


def test_invocation_and_terminal_failure_share_diagnostic_identity(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    error = RuntimeError("token=super-secret agent process exited")
    execution = parts.controller.start_agent_execution("implementer", "round 5", "prompt")
    parts.controller.after_agent(
        "implementer", "round 5", error=error, execution_id=execution.execution_id
    )
    parts.controller.finish(error)

    execution_event, phase, terminal = [
        event
        for event in parts.journal.read()
        if event.type
        in {
            EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
            EventType.EVENT_TYPE_PHASE_FINISHED,
            EventType.EVENT_TYPE_RUN_FAILED,
        }
    ]
    assert execution_event.HasField("diagnostic")
    assert phase.diagnostic == execution_event.diagnostic
    assert terminal.HasField("diagnostic")
    assert terminal.diagnostic.id == execution_event.diagnostic.id
    assert terminal.diagnostic.scope == DiagnosticScope.DIAGNOSTIC_SCOPE_INVOCATION
    assert execution_event.diagnostic.severity == DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR
    assert terminal.diagnostic.severity == DiagnosticSeverity.DIAGNOSTIC_SEVERITY_FATAL
    assert (
        terminal.diagnostic.retryability == DiagnosticRetryability.DIAGNOSTIC_RETRYABILITY_UNKNOWN
    )
    assert execution_event.WhichOneof("data") == "agent_execution_finished"
    assert execution_event.agent_execution_finished.error == "Agent execution failed"
    assert terminal.diagnostic.detail == ("RuntimeError: token=[REDACTED] agent process exited")


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.EVENT_TYPE_CONFIGURATION_FAILED,
        EventType.EVENT_TYPE_INVOCATION_FINISHED,
        EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
        EventType.EVENT_TYPE_PHASE_FINISHED,
        EventType.EVENT_TYPE_RUN_FAILED,
        EventType.EVENT_TYPE_RUN_INTERRUPTED,
    ],
)
def test_operational_failure_events_require_diagnostics(
    tmp_path: Path, event_type: EventType
) -> None:
    parts = build_server_parts(tmp_path)
    with pytest.raises(ValueError, match="must include a diagnostic"):
        parts.journal.record(event_type, status=EventStatus.EVENT_STATUS_FAILED)
    with pytest.raises(ValueError, match="must include a diagnostic"):
        parts.journal.append(
            messages.make_event(event_type, "boom", status=EventStatus.EVENT_STATUS_FAILED)
        )


def test_append_accepts_failed_gate_outcomes_without_diagnostics(tmp_path):  # noqa: ANN001, ANN201
    # A failed gate is an expected semantic outcome, not an operational fault,
    # so the append invariant must leave it diagnostic-less.
    parts = build_server_parts(tmp_path)
    gate = parts.journal.append(
        messages.make_event(
            EventType.EVENT_TYPE_GATE_FINISHED,
            status=EventStatus.EVENT_STATUS_FAILED,
            data=events_pb2.GateFinishedData(
                gate=events_pb2.GateKind.GATE_KIND_ACCURACY, output_tail="mismatch"
            ),
        )
    )
    assert not gate.HasField("diagnostic")


def test_semantic_failure_events_do_not_require_diagnostics(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    judge = parts.journal.record(
        EventType.EVENT_TYPE_JUDGE_RESULT,
        status=EventStatus.EVENT_STATUS_FAILED,
        data=events_pb2.JudgeResultData(
            verdict=events_pb2.JudgeVerdict.JUDGE_VERDICT_FAIL, feedback="incorrect", attempt=1
        ),
    )
    round_finished = parts.journal.record(
        EventType.EVENT_TYPE_ROUND_FINISHED,
        status=EventStatus.EVENT_STATUS_FAILED,
        data=events_pb2.RoundFinishedData(
            attempts=1, judge_verdict=events_pb2.RoundJudgeVerdict.ROUND_JUDGE_VERDICT_FAIL
        ),
    )
    assert not judge.HasField("diagnostic")
    assert not round_finished.HasField("diagnostic")


def test_capture_failure_emits_nothing_on_success(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    before = parts.journal.read()
    with parts.journal.capture_failure(
        event_type=EventType.EVENT_TYPE_PHASE_FINISHED,
        scope=DiagnosticScope.DIAGNOSTIC_SCOPE_PHASE,
        operation="Background maintenance",
    ):
        pass
    assert parts.journal.read() == before


def test_capture_failure_records_once_and_reraises(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    error = KeyboardInterrupt("background worker stopped")
    before = parts.journal.read()

    with (
        pytest.raises(KeyboardInterrupt) as raised,
        parts.journal.capture_failure(
            event_type=EventType.EVENT_TYPE_PHASE_FINISHED,
            scope=DiagnosticScope.DIAGNOSTIC_SCOPE_PHASE,
            operation="Background maintenance",
            data=events_pb2.PhaseData(phase="maintenance", attempt=1),
            agent_kind="maintenance",
            round_label="round 1",
        ),
    ):
        raise error

    assert raised.value is error
    events = parts.journal.read()
    assert len(events) == len(before) + 1
    captured = events[-1]
    assert captured.status == EventStatus.EVENT_STATUS_FAILED
    assert captured.phase == events_pb2.PhaseData(phase="maintenance", attempt=1)
    assert captured.HasField("diagnostic")
    assert captured.diagnostic.summary == "Background maintenance failed"
    assert not any(event.type == EventType.EVENT_TYPE_RUN_FAILED for event in events)


def test_nonterminal_failure_helpers_reject_wrong_event_owners(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    before = parts.journal.read()
    with pytest.raises(ValueError, match="without owning run termination"):
        parts.journal.record_failure(
            EventType.EVENT_TYPE_JUDGE_RESULT,
            RuntimeError("incorrect result"),
            scope=DiagnosticScope.DIAGNOSTIC_SCOPE_PHASE,
            operation="Judge",
        )
    for event_type in (EventType.EVENT_TYPE_CONFIGURATION_FAILED, EventType.EVENT_TYPE_RUN_FAILED):
        with (
            pytest.raises(ValueError, match="without owning run termination"),
            parts.journal.capture_failure(
                event_type=event_type,
                scope=DiagnosticScope.DIAGNOSTIC_SCOPE_RUN,
                operation="Background maintenance",
            ),
        ):
            raise RuntimeError("worker failed")  # noqa: TRY003
    assert parts.journal.read() == before


def test_terminal_wrapper_reuses_cause_diagnostic(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    cause = RuntimeError("token=super-secret agent process exited")
    execution = parts.controller.start_agent_execution("implementer", "round 5", "prompt")
    parts.controller.after_agent(
        "implementer", "round 5", error=cause, execution_id=execution.execution_id
    )
    wrapper = RuntimeError("run cleanup failed")
    wrapper.__cause__ = cause
    parts.controller.finish(wrapper)

    execution_event, terminal = [
        event
        for event in parts.journal.read()
        if event.type
        in {EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED, EventType.EVENT_TYPE_RUN_FAILED}
    ]
    assert execution_event.HasField("diagnostic")
    assert terminal.HasField("diagnostic")
    assert terminal.diagnostic.id == execution_event.diagnostic.id
    assert terminal.diagnostic.detail == (
        "RuntimeError: run cleanup failed <- RuntimeError: token=[REDACTED] agent process exited"
    )
