"""Durable journal attachment, replay translation, diagnostics, and failure-helper tests."""

from __future__ import annotations

import itertools
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.server.support import HARNESS_RUN_ID, build_server_parts

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vs_sim.api import Condition

from server.diagnostics import (
    Diagnostic,
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
)
from server.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    ConfigurationFailedData,
    EventData,
    EventHeader,
    EventStatus,
    EventType,
    GateFinishedData,
    GateKind,
    JudgeResultData,
    PhaseData,
    RoundFinishedData,
    RunEvent,
    make_event,
)
from server.host_api import FailureReason
from server.journal import WireJournal
from server.runtime import failure_reason
from vs_sim.api.testing import ManualClock, SimThreads, examples


class _RecordingCondition:
    """A single-threaded condition Fake that records committed notifications."""

    def __init__(self) -> None:
        self.notifications = 0

    def __enter__(self) -> _RecordingCondition:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def notify_all(self) -> None:
        self.notifications += 1


class _ListenerFailedError(RuntimeError):
    """A scripted post-commit projection failure."""


def _recording_journal(
    condition: _RecordingCondition,
    *,
    listener_errors: (
        list[tuple[Exception, EventHeader]] | Callable[[Exception, EventHeader], None] | None
    ) = None,
) -> WireJournal:
    threads = SimThreads()
    if listener_errors is None:
        return WireJournal(
            cast("Condition", condition),
            clock=ManualClock(),
            threads=threads,
        )
    if isinstance(listener_errors, list):

        def error_sink(error: Exception, header: EventHeader) -> None:
            listener_errors.append((error, header))

    else:
        error_sink = listener_errors
    return WireJournal(
        cast("Condition", condition),
        clock=ManualClock(),
        threads=threads,
        listener_errors=error_sink,
    )


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _write_stored_events(log_dir: Path, events: Sequence[RunEvent]) -> None:
    log_dir.mkdir()
    (log_dir / "run-events.jsonl").write_text(
        "".join(event.model_dump_json() + "\n" for event in events)
    )


def _stored_event(
    sequence: int, event_type: EventType, status: EventStatus, data: EventData
) -> RunEvent:
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


def test_committed_event_survives_a_live_listener_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed projection cannot turn a durable append into a reported failure."""
    condition = _RecordingCondition()
    journal = _recording_journal(condition)
    journal.attach(tmp_path, run_id="run-a")
    later_sequences: list[int] = []
    failure = _ListenerFailedError("registry publish failed")

    def broken(_event: RunEvent) -> None:
        raise failure

    journal.add_listener(broken, replay_filter=lambda _header: False)
    journal.add_listener(
        lambda event: later_sequences.append(event.sequence),
        replay_filter=lambda _header: False,
    )
    notifications_before = condition.notifications

    with caplog.at_level(logging.ERROR, logger="server.journal"):
        committed = journal.record(EventType.SERVER_READY)

    assert journal.read_history()[-1] == committed
    assert later_sequences == [committed.sequence]
    assert condition.notifications == notifications_before + 1
    assert any(
        "observer failed while applying committed server_ready event" in record.getMessage()
        for record in caplog.records
    )


def test_replay_attempts_every_listener_after_one_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One broken replay projection cannot starve independent projections."""
    stored = _recording_journal(_RecordingCondition())
    stored.attach(tmp_path, run_id="run-a")
    stored.record(EventType.SERVER_READY)
    expected_sequences = [event.sequence for event in stored.read_history()]

    replay = _recording_journal(_RecordingCondition())
    later_sequences: list[int] = []
    failure = _ListenerFailedError("replay projection failed")

    def broken(_event: RunEvent) -> None:
        raise failure

    replay.add_listener(broken, replay_filter=lambda _header: True)
    replay.add_listener(
        lambda event: later_sequences.append(event.sequence),
        replay_filter=lambda _header: True,
    )

    with caplog.at_level(logging.ERROR, logger="server.journal"):
        replay.attach(tmp_path, run_id="run-a", read_only=True)

    assert later_sequences == expected_sequences
    assert sum(
        "observer failed while applying committed" in record.getMessage()
        for record in caplog.records
    ) == len(expected_sequences)


def test_listener_failures_reach_the_explicit_error_owner(tmp_path: Path) -> None:
    """The journal reports observer faults separately from its append result."""
    failures: list[tuple[Exception, EventHeader]] = []
    journal = _recording_journal(_RecordingCondition(), listener_errors=failures)
    journal.attach(tmp_path, run_id="run-a")
    failure = _ListenerFailedError("registry publish failed")

    def broken(_event: RunEvent) -> None:
        raise failure

    journal.add_listener(broken, replay_filter=lambda _header: False)

    committed = journal.record(EventType.SERVER_READY)

    assert failures == [(failure, _header_from_event_for_test(committed))]


def test_failed_replay_filter_does_not_starve_other_reducers(tmp_path: Path) -> None:
    """A replay-selection failure is isolated like a listener failure."""
    stored = _recording_journal(_RecordingCondition())
    stored.attach(tmp_path, run_id="run-a")
    stored.record(EventType.SERVER_READY)
    expected = stored.read_history()
    failures: list[tuple[Exception, EventHeader]] = []
    replay = _recording_journal(_RecordingCondition(), listener_errors=failures)
    selected_sequences: list[int] = []
    failure = _ListenerFailedError("replay filter failed")

    def broken_filter(_header: EventHeader) -> bool:
        raise failure

    replay.add_listener(lambda _event: None, replay_filter=broken_filter)
    replay.add_listener(
        lambda event: selected_sequences.append(event.sequence),
        replay_filter=lambda _header: True,
    )

    replay.attach(tmp_path, run_id="run-a", read_only=True)

    assert selected_sequences == [event.sequence for event in expected]
    assert failures == [(failure, _header_from_event_for_test(event)) for event in expected]


def test_failed_listener_error_sink_cannot_escape_or_starve_delivery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Even the final diagnostic boundary cannot rewrite the append outcome."""
    condition = _RecordingCondition()
    sink_failure = _ListenerFailedError("diagnostic sink failed")

    def broken_sink(_error: Exception, _header: EventHeader) -> None:
        raise sink_failure

    journal = _recording_journal(condition, listener_errors=broken_sink)
    journal.attach(tmp_path, run_id="run-a")
    observed: list[int] = []

    def broken_listener(_event: RunEvent) -> None:
        raise _ListenerFailedError

    journal.add_listener(broken_listener, replay_filter=lambda _header: False)
    journal.add_listener(
        lambda event: observed.append(event.sequence), replay_filter=lambda _header: False
    )
    notifications_before = condition.notifications

    with caplog.at_level(logging.ERROR, logger="server.journal"):
        committed = journal.record(EventType.SERVER_READY)

    assert observed == [committed.sequence]
    assert condition.notifications == notifications_before + 1
    assert any("observer error sink failed" in record.getMessage() for record in caplog.records)


def test_reentrant_listener_error_sink_cannot_reenter_itself(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A diagnostic append runs after fan-out and cannot recursively invoke its sink."""
    condition = _RecordingCondition()
    sink_sequences: list[int] = []
    observed: list[int] = []
    journal: WireJournal

    def append_diagnostic(_error: Exception, header: EventHeader) -> None:
        sink_sequences.append(header.sequence)
        journal.record(EventType.OUTPUT, text="observer diagnostic")

    journal = _recording_journal(condition, listener_errors=append_diagnostic)
    journal.attach(tmp_path, run_id="run-a")

    def broken_listener(_event: RunEvent) -> None:
        raise _ListenerFailedError

    journal.add_listener(broken_listener, replay_filter=lambda _header: False)
    journal.add_listener(
        lambda event: observed.append(event.sequence), replay_filter=lambda _header: False
    )
    notifications_before = condition.notifications

    with caplog.at_level(logging.ERROR, logger="server.journal"):
        committed = journal.record(EventType.SERVER_READY)

    appended = journal.read_history()[-2:]
    assert [event.type for event in appended] == [EventType.SERVER_READY, EventType.OUTPUT]
    assert sink_sequences == [committed.sequence]
    assert observed == [event.sequence for event in appended]
    assert condition.notifications == notifications_before + 2
    assert any(
        "observer failed while applying committed output event" in record.getMessage()
        for record in caplog.records
    )


def _header_from_event_for_test(event: RunEvent) -> EventHeader:
    """Build the public observer-failure context expected for one committed event."""
    return EventHeader(
        sequence=event.sequence,
        type=event.type,
        execution_id=event.execution_id,
        chat_thread_id=event.chat_thread_id,
        run_id=event.run_id,
    )


def test_bootstrap_events_join_durable_history(tmp_path: Path) -> None:
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


def test_attach_renumbering_bootstrap_events_starts_a_new_sequence_space(tmp_path: Path) -> None:
    durable = build_server_parts(tmp_path / "durable")
    durable.journal.record(EventType.RUN_FINISHED, status=EventStatus.COMPLETED)

    current = build_server_parts(tmp_path / "bootstrap")
    current.journal.record(EventType.SERVER_READY, status=EventStatus.ACTIVE)
    bootstrap_store = current.journal.store_id_locked()
    folded = current.journal.read()
    current.attach(tmp_path / "durable")
    attached = current.journal.read()[: len(folded)]

    # The bootstrap events were re-appended onto the durable log's tail, so the
    # sequences a subscriber already folded now name different events. That is
    # a different sequence space and the identity has to say so.
    assert current.journal.store_id_locked() != bootstrap_store
    assert [event.sequence for event in attached] == [event.sequence for event in folded]
    assert [event.type for event in attached] != [event.type for event in folded]


def test_attach_into_an_empty_log_keeps_the_sequence_space(tmp_path: Path) -> None:
    current = build_server_parts(tmp_path / "bootstrap")
    bootstrap_store = current.journal.store_id_locked()
    before = [(event.sequence, event.type) for event in current.journal.read()]

    current.attach(tmp_path / "durable")

    # Nothing was renumbered, so every folded sequence still names the same
    # event and a subscriber has nothing to re-fold.
    assert current.journal.store_id_locked() == bootstrap_store
    assert [(event.sequence, event.type) for event in current.journal.read()] == before


def test_phase_only_legacy_replay_translates_in_place(tmp_path: Path) -> None:
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


def test_modern_phase_events_replay_unchanged(tmp_path: Path) -> None:
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


def test_invocation_and_terminal_failure_share_diagnostic_identity(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    error = RuntimeError("token=super-secret agent process exited")
    execution = parts.start_execution("implementer", "round 5", "prompt")
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
        EventType.AGENT_EXECUTION_FINISHED,
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
    with pytest.raises(ValueError, match="must include a diagnostic"):
        parts.journal.append(
            make_event(
                event_type,
                "boom",
                status=EventStatus.FAILED,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )


def test_append_accepts_failed_gate_outcomes_without_diagnostics(tmp_path: Path) -> None:
    # A failed gate is an expected semantic outcome, not an operational fault,
    # so the append invariant must leave it diagnostic-less.
    parts = build_server_parts(tmp_path)
    gate = parts.journal.append(
        make_event(
            EventType.GATE_FINISHED,
            status=EventStatus.FAILED,
            data=GateFinishedData(gate=GateKind.ACCURACY, output_tail="mismatch"),
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    assert gate.diagnostic is None


def test_semantic_failure_events_do_not_require_diagnostics(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    judge = parts.journal.record(
        EventType.JUDGE_RESULT,
        status=EventStatus.FAILED,
        data=JudgeResultData(verdict="fail", feedback="incorrect", attempt=1),
    )
    round_finished = parts.journal.record(
        EventType.ROUND_FINISHED,
        status=EventStatus.FAILED,
        data=RoundFinishedData(
            attempts=1,
            judge_verdict="fail",
            profile_skipped=False,
        ),
    )
    assert judge.diagnostic is None
    assert round_finished.diagnostic is None


def test_capture_failure_emits_nothing_on_success(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    before = parts.journal.read()
    with parts.journal.capture_failure(
        event_type=EventType.PHASE_FINISHED,
        scope=DiagnosticScope.PHASE,
        operation="Background maintenance",
    ):
        pass
    assert parts.journal.read() == before


def test_capture_failure_records_once_and_reraises(tmp_path: Path) -> None:
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


def test_nonterminal_failure_helpers_reject_wrong_event_owners(tmp_path: Path) -> None:
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
        error = RuntimeError("worker failed")
        with (
            pytest.raises(ValueError, match="without owning run termination"),
            parts.journal.capture_failure(
                event_type=event_type,
                scope=DiagnosticScope.RUN,
                operation="Background maintenance",
            ),
        ):
            raise error
    assert parts.journal.read() == before


def test_terminal_wrapper_reuses_cause_diagnostic(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    cause = RuntimeError("token=super-secret agent process exited")
    execution = parts.start_execution("implementer", "round 5", "prompt")
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


def _legacy_driver_line(sequence: int, event_type: str, data: Mapping[str, object]) -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "sequence": sequence,
            "run_id": "persisted-run",
            "timestamp": "2024-01-01T00:00:00Z",
            "type": event_type,
            "agent_kind": "implementer",
            "round_label": "round 1",
            "execution_id": "exec-1",
            "data": data,
        }
    )


def _write_replayed_chat_thread(log_dir: Path, thread_id: str) -> None:
    """Write one pre-validation thread record through the durable wire shape."""
    created = {
        "kind": "chat_thread_created",
        "thread_id": thread_id,
        "provider": "codex",
        "model": "gpt-test",
        "created_at": "2024-01-01T00:00:00Z",
    }
    record = json.loads(_legacy_driver_line(1, "chat_thread_created", created))
    record["chat_thread_id"] = thread_id
    log_dir.mkdir()
    (log_dir / "run-events.jsonl").write_text(json.dumps(record) + "\n")


def test_replayed_absolute_chat_thread_identity_fails_before_attachment(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    log_dir = tmp_path / "logs"
    thread_id = str(outside)
    _write_replayed_chat_thread(log_dir, thread_id)

    with pytest.raises(ValueError, match="invalid experiment chat thread identity") as error:
        build_server_parts(log_dir)

    assert repr(thread_id) in str(error.value)
    assert not outside.exists()


@pytest.mark.parametrize(
    "thread_id",
    [
        pytest.param("../outside", id="parent-traversal"),
        pytest.param("nested/thread", id="posix-separator"),
        pytest.param(r"nested\thread", id="windows-separator"),
        pytest.param(r"C:\outside", id="windows-drive"),
        pytest.param(r"\\server\share", id="windows-unc"),
        pytest.param("con", id="windows-device"),
        pytest.param("nul.txt", id="windows-device-extension"),
        pytest.param("thread.", id="windows-trailing-dot"),
        pytest.param("thread ", id="windows-trailing-space"),
    ],
)
def test_replayed_nonportable_chat_thread_identity_fails_before_attachment(
    tmp_path: Path, thread_id: str
) -> None:
    log_dir = tmp_path / "logs"
    _write_replayed_chat_thread(log_dir, thread_id)

    with pytest.raises(ValueError, match="invalid experiment chat thread identity") as error:
        build_server_parts(log_dir)

    assert repr(thread_id) in str(error.value)
    assert list(tmp_path.rglob("conversation.jsonl")) == []


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=examples(pr=10, nightly=25),
)
@given(driver=st.one_of(st.none(), st.text(max_size=12)))
def test_journal_recorded_with_the_removed_driver_field_still_replays(
    tmp_path_factory: pytest.TempPathFactory, driver: str | None
) -> None:
    """Events written while a ``driver`` attribution existed replay without it.

    The field was always ``"agentshim"`` and was removed from the wire. Event
    payloads ignore keys they no longer declare, so history recorded before the
    removal loads and the dropped key never reappears on the wire. The event
    envelope still rejects unknown keys.
    """
    log_dir = tmp_path_factory.mktemp("legacy-driver") / "logs"
    log_dir.mkdir()
    started = {
        "kind": "agent_execution_started",
        "stage": "implementer",
        "activity": {
            "kind": "agent_execution_activity_changed",
            "mode": "thinking",
            "summary": "working",
        },
        "driver": driver,
        "provider": "codex",
        "model": "gpt-test",
    }
    created = {
        "kind": "chat_thread_created",
        "thread_id": "thread-1",
        "driver": driver or "agentshim",
        "provider": "codex",
        "model": "gpt-test",
        "created_at": "2024-01-01T00:00:00Z",
    }
    (log_dir / "run-events.jsonl").write_text(
        _legacy_driver_line(1, "agent_execution_started", started)
        + "\n"
        + _legacy_driver_line(2, "chat_thread_created", created)
        + "\n"
    )

    parts = build_server_parts(log_dir)
    events = parts.journal.read()

    started_events = [e.data for e in events if isinstance(e.data, AgentExecutionStartedData)]
    assert [data.provider for data in started_events] == ["codex"]
    assert all("driver" not in (event.model_dump(mode="json")["data"] or {}) for event in events)
    assert [thread.thread_id for thread in parts.chat.threads()] == ["thread-1"]


_EXAMPLES = itertools.count()


@settings(
    max_examples=examples(pr=10, nightly=25),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    socket_parent=st.sampled_from(["runs", "T", "tmp", "vibesys-session-x"]),
    early_events=st.integers(min_value=0, max_value=4),
)
def test_every_event_names_the_run_from_the_first(
    tmp_path: Path, socket_parent: str, early_events: int
) -> None:
    # Regression: a detached server journaled into its control-socket directory
    # before its run was ready and stamped those events with that directory's
    # parent name. A client latches the first run id it folds, so it ignored
    # every event the run sent later. The server now knows its run from the
    # start, so no event can name another one, whatever the socket's path.
    case = tmp_path / str(next(_EXAMPLES))
    current = build_server_parts(case / socket_parent / "instance")
    for _ in range(early_events):
        current.journal.record(EventType.SERVER_READY, status=EventStatus.ACTIVE)

    current.journal.attach(case / "run-logs", run_id=HARNESS_RUN_ID)
    current.journal.record(EventType.RUN_STARTED, status=EventStatus.ACTIVE)

    assert {event.run_id for event in current.journal.read()} == {HARNESS_RUN_ID}


@pytest.mark.parametrize(
    ("run_id", "message"), [(None, "must name"), ("", "must name"), ("other", "cannot continue")]
)
def test_a_writable_journal_never_drops_or_changes_its_run(
    tmp_path: Path, run_id: str | None, message: str
) -> None:
    current = build_server_parts(tmp_path / "instance")

    with pytest.raises(ValueError, match=message):
        current.journal.attach(tmp_path / "run-logs", run_id=run_id)


def test_unknown_event_envelope_keys_stay_rejected() -> None:
    with pytest.raises(ValueError, match="driver"):
        RunEvent.model_validate_json(
            _legacy_driver_line(1, "output", {"kind": "agent_output_chunk"}).replace(
                '"sequence"', '"driver": "agentshim", "sequence"', 1
            )
        )


@pytest.mark.parametrize(
    ("event_type", "data", "stage"),
    [
        (EventType.RUN_FAILED, None, "run"),
        (EventType.RUN_INTERRUPTED, None, "run"),
        (
            EventType.CONFIGURATION_FAILED,
            ConfigurationFailedData(code="c", stage="resume_resolution", message="m", exit_code=2),
            "resume_resolution",
        ),
    ],
)
def test_the_event_a_run_failed_with_names_its_reason(
    tmp_path: Path, event_type: EventType, data: ConfigurationFailedData | None, stage: str
) -> None:
    current = build_server_parts(tmp_path / "instance")
    diagnostic = Diagnostic(
        code="sandbox_start_failed", summary="no GPU", scope=DiagnosticScope.RUN
    )
    current.journal.record(event_type, status=EventStatus.FAILED, data=data, diagnostic=diagnostic)
    current.journal.record(EventType.OUTPUT, data=None)

    *_, failed, output = current.journal.read()

    assert failure_reason(failed) == FailureReason(
        code="sandbox_start_failed", stage=stage, message="no GPU"
    )
    assert failure_reason(output) is None
