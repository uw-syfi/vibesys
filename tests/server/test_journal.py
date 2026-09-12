"""Durable journal attachment, replay translation, diagnostics, and failure-helper tests."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.server.support import build_server_parts

if TYPE_CHECKING:
    from pathlib import Path

from server.diagnostics import (
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
)
from server.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    EventStatus,
    EventType,
    JudgeResultData,
    PhaseData,
    RoundFinishedData,
    RunEvent,
)


def _events(path):  # noqa: ANN001, ANN202
    return [json.loads(line) for line in path.read_text().splitlines()]


def _write_stored_events(log_dir, events):  # noqa: ANN001, ANN202
    log_dir.mkdir()
    (log_dir / "run-events.jsonl").write_text(
        "".join(event.model_dump_json() + "\n" for event in events)
    )


def _stored_event(sequence, event_type, status, data):  # noqa: ANN001, ANN202
    return RunEvent(
        sequence=sequence,
        run_id="persisted-run",
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        type=event_type,
        status=status,
        agent_kind="implementer",
        round_label="round 1",
        execution_id="exec-1",
        data=data,
    )


def test_bootstrap_events_join_durable_history(tmp_path):  # noqa: ANN001, ANN201
    durable = build_server_parts(tmp_path / "durable")
    durable.journal.record(EventType.RUN_FINISHED, status=EventStatus.COMPLETED)

    current = build_server_parts(tmp_path / "bootstrap")
    current.journal.record(EventType.SERVER_READY, status=EventStatus.ACTIVE)
    current.attach(tmp_path / "durable")
    current.journal.record(EventType.RUN_STARTED, status=EventStatus.ACTIVE)

    # Each attach publishes the starting -> running transition, and the second
    # session's lands after the first session's terminal event: a client that
    # folds this history in order ends at `running`, not at the old `completed`.
    expected = [
        EventType.SERVER_STARTED,
        EventType.RUN_STATUS_CHANGED,
        EventType.RUN_FINISHED,
        EventType.SERVER_STARTED,
        EventType.RUN_STATUS_CHANGED,
        EventType.SERVER_READY,
        EventType.RUN_STARTED,
    ]
    assert [event.type for event in current.journal.read()] == expected
    assert [event["type"] for event in _events(tmp_path / "durable/run-events.jsonl")] == [
        event.value for event in expected
    ]


def test_phase_only_legacy_replay_translates_in_place(tmp_path):  # noqa: ANN001, ANN201
    """A translated phase event replaces the original at its stored sequence."""
    log_dir = tmp_path / "legacy"
    _write_stored_events(
        log_dir,
        [
            _stored_event(
                1,
                EventType.PHASE_STARTED,
                EventStatus.ACTIVE,
                PhaseData(phase="implementer", attempt=1),
            ),
            _stored_event(
                2,
                EventType.PHASE_FINISHED,
                EventStatus.COMPLETED,
                PhaseData(phase="implementer", attempt=1),
            ),
        ],
    )

    parts = build_server_parts(log_dir)
    events = parts.journal.read()

    sequences = [event.sequence for event in events]
    assert all(left < right for left, right in itertools.pairwise(sequences)), sequences
    started, finished = events[0], events[1]
    assert started.type is EventType.AGENT_EXECUTION_STARTED
    assert started.sequence == 1
    assert isinstance(started.data, AgentExecutionStartedData)
    assert started.data.stage == "implementer"
    assert started.data.attempt == 1
    assert finished.type is EventType.AGENT_EXECUTION_FINISHED
    assert finished.sequence == 2
    assert finished.status is EventStatus.COMPLETED
    assert not any(
        event.type in {EventType.PHASE_STARTED, EventType.PHASE_FINISHED} for event in events
    )


def test_modern_phase_events_replay_unchanged(tmp_path):  # noqa: ANN001, ANN201
    """Phase events with a canonical lifecycle sibling pass through untouched."""
    log_dir = tmp_path / "modern"
    activity = AgentExecutionActivityData(mode="thinking", summary="Implementing")
    stored = [
        _stored_event(
            1,
            EventType.AGENT_EXECUTION_STARTED,
            EventStatus.ACTIVE,
            AgentExecutionStartedData(stage="implementer", attempt=1, activity=activity),
        ),
        _stored_event(
            2,
            EventType.PHASE_STARTED,
            EventStatus.ACTIVE,
            PhaseData(phase="implementer", attempt=1),
        ),
        _stored_event(
            3,
            EventType.AGENT_EXECUTION_FINISHED,
            EventStatus.COMPLETED,
            AgentExecutionFinishedData(result={"ok": True}),
        ),
        _stored_event(
            4,
            EventType.PHASE_FINISHED,
            EventStatus.COMPLETED,
            PhaseData(phase="implementer", attempt=1),
        ),
    ]
    _write_stored_events(log_dir, stored)

    parts = build_server_parts(log_dir)
    events = parts.journal.read()

    assert [event.model_dump_json() for event in events[: len(stored)]] == [
        event.model_dump_json() for event in stored
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
            EventType.AGENT_EXECUTION_FINISHED,
            EventType.PHASE_FINISHED,
            EventType.RUN_FAILED,
        }
    ]
    assert execution_event.diagnostic is not None
    assert phase.diagnostic == execution_event.diagnostic
    assert terminal.diagnostic is not None
    assert terminal.diagnostic.id == execution_event.diagnostic.id
    assert terminal.diagnostic.scope is DiagnosticScope.INVOCATION
    assert execution_event.diagnostic.severity is DiagnosticSeverity.ERROR
    assert terminal.diagnostic.severity is DiagnosticSeverity.FATAL
    assert terminal.diagnostic.retryability is DiagnosticRetryability.UNKNOWN
    assert isinstance(execution_event.data, AgentExecutionFinishedData)
    assert execution_event.data.error == "Agent execution failed"
    assert terminal.diagnostic.detail == ("RuntimeError: token=[REDACTED] agent process exited")


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.CONFIGURATION_FAILED,
        EventType.INVOCATION_FINISHED,
        EventType.PHASE_FINISHED,
        EventType.RUN_FAILED,
        EventType.RUN_INTERRUPTED,
    ],
)
def test_operational_failure_events_require_diagnostics(
    tmp_path: Path, event_type: EventType
) -> None:
    parts = build_server_parts(tmp_path)
    for status in (EventStatus.FAILED, "failed"):
        with pytest.raises(ValueError, match="must include a diagnostic"):
            parts.journal.record(event_type, status=status)


def test_semantic_failure_events_do_not_require_diagnostics(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    judge = parts.journal.record(
        EventType.JUDGE_RESULT,
        status=EventStatus.FAILED,
        data=JudgeResultData(verdict="fail", feedback="incorrect", attempt=1),
    )
    round_finished = parts.journal.record(
        EventType.ROUND_FINISHED,
        status=EventStatus.FAILED,
        data=RoundFinishedData(attempts=1, judge_verdict="fail"),
    )
    assert judge.diagnostic is None
    assert round_finished.diagnostic is None


def test_capture_failure_emits_nothing_on_success(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    before = parts.journal.read()
    with parts.journal.capture_failure(
        event_type=EventType.PHASE_FINISHED,
        scope=DiagnosticScope.PHASE,
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
            event_type=EventType.PHASE_FINISHED,
            scope=DiagnosticScope.PHASE,
            operation="Background maintenance",
            data=PhaseData(phase="maintenance", attempt=1),
            agent_kind="maintenance",
            round_label="round 1",
        ),
    ):
        raise error

    assert raised.value is error
    events = parts.journal.read()
    assert len(events) == len(before) + 1
    captured = events[-1]
    assert captured.status is EventStatus.FAILED
    assert captured.data == PhaseData(phase="maintenance", attempt=1)
    assert captured.diagnostic is not None
    assert captured.diagnostic.summary == "Background maintenance failed"
    assert not any(event.type is EventType.RUN_FAILED for event in events)


def test_nonterminal_failure_helpers_reject_wrong_event_owners(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path)
    before = parts.journal.read()
    with pytest.raises(ValueError, match="without owning run termination"):
        parts.journal.record_failure(
            EventType.JUDGE_RESULT,
            RuntimeError("incorrect result"),
            scope=DiagnosticScope.PHASE,
            operation="Judge",
        )
    for event_type in (EventType.CONFIGURATION_FAILED, EventType.RUN_FAILED):
        with (
            pytest.raises(ValueError, match="without owning run termination"),
            parts.journal.capture_failure(
                event_type=event_type,
                scope=DiagnosticScope.RUN,
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
        if event.type in {EventType.AGENT_EXECUTION_FINISHED, EventType.RUN_FAILED}
    ]
    assert execution_event.diagnostic is not None
    assert terminal.diagnostic is not None
    assert terminal.diagnostic.id == execution_event.diagnostic.id
    assert terminal.diagnostic.detail == (
        "RuntimeError: run cleanup failed <- RuntimeError: token=[REDACTED] agent process exited"
    )
