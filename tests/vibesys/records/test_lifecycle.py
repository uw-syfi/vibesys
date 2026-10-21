"""Native lifecycle follows the legacy fold and settles unfinished work."""

from datetime import UTC, datetime

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from server.host_api import RunStatus
from server.host_run_summary import RunTerminalKind
from server.run_lifecycle import RunState, RunTrigger, transition
from server.run_summary import RunSummaryFold
from vibesys.records.api import MappingState, map_event
from vs_core.api import EndReason


def event(kind: e.CoreEventType, data: e.CoreEventData | None = None) -> e.CoreEvent:
    return e.CoreEvent(
        type=kind, data=data, run_id="run-a", timestamp=datetime(2026, 1, 1, tzinfo=UTC)
    )


@pytest.mark.regression
@example(cycles=0, failed=True, structured=False, ending=True)
@example(cycles=0, failed=False, structured=False, ending=False)
@given(
    cycles=st.integers(0, 3), failed=st.booleans(), structured=st.booleans(), ending=st.booleans()
)
def test_native_lifecycle_agrees_with_legacy_status_over_event_sequences(
    cycles: int, *, failed: bool, structured: bool, ending: bool
) -> None:
    observations = [
        event(e.CoreEventType.RUN_STARTED, e.RunStartedData(outer_loop="dynamic", input="input"))
    ]
    triggers = [RunTrigger.READY]
    for _ in range(cycles):
        observations.extend(
            event(kind)
            for kind in (
                e.CoreEventType.PAUSE_REQUESTED,
                e.CoreEventType.PAUSED,
                e.CoreEventType.RESUMED,
            )
        )
        triggers.extend(
            (RunTrigger.PAUSE_REQUESTED, RunTrigger.INVOCATION_FINISHED, RunTrigger.RESUMED)
        )
    if ending:
        observations.append(
            event(
                e.CoreEventType.RUN_ENDING,
                e.RunEndingData(
                    reason=EndReason.STOPPED,
                    summary="stopped",
                    resumable=True,
                    rounds_finished=0,
                ),
            )
        )
        triggers.append(RunTrigger.INVOCATION_FINISHED)
    payload = (
        e.RunFailedData(
            failure=e.RunFailure(
                kind=e.RunFailureKind.NO_RESULT,
                reason="no result",
                workstreams_started=0,
                workstream_budget=1,
                candidates_kept=0,
            )
        )
        if failed and structured
        else None
    )
    observations.append(
        event(e.CoreEventType.RUN_FAILED if failed else e.CoreEventType.RUN_FINISHED, payload)
    )
    triggers.append(RunTrigger.FAILED if failed else RunTrigger.COMPLETED)
    native = MappingState("run-a")
    legacy = RunState(RunStatus.STARTING)
    fold = RunSummaryFold()
    lifecycle = None
    for sequence, (source, trigger) in enumerate(zip(observations, triggers, strict=True), start=1):
        observation = source.model_copy(update={"sequence": sequence})
        result = map_event(native, observation)
        native = result.state
        legacy = transition(legacy, trigger)
        fold.consume(observation.model_dump_json().encode())
        for record in result.records:
            p.validate(record)
            if record.body and record.body.field == "lifecycle":
                lifecycle = record.body.value.state
        assert lifecycle is not None
        if observation.type not in {e.CoreEventType.RUN_FINISHED, e.CoreEventType.RUN_FAILED}:
            assert lifecycle.field == "running"
            assert isinstance(lifecycle.value, p.Running)
            assert (lifecycle.value.hold is not None) is (
                legacy.status in {RunStatus.PAUSING, RunStatus.PAUSED}
            )
            assert not legacy.status.has_ended
    assert fold.terminal is not None
    assert lifecycle is not None
    assert lifecycle.field == "ended"
    assert (lifecycle.value.outcome == p.Outcome.FAILED) is (
        fold.terminal.kind is RunTerminalKind.FAILED
    )
    assert lifecycle.value.outcome == (
        p.Outcome.FAILED if failed else p.Outcome.STOPPED if ending else p.Outcome.FINISHED
    )
    assert legacy.status.has_ended


@pytest.mark.regression
@pytest.mark.parametrize(
    "terminal", [e.CoreEventType.RUN_FAILED, e.CoreEventType.RUN_FINISHED, e.CoreEventType.STOPPED]
)
def test_ended_run_has_no_running_waiting_or_pending_spans(terminal: e.CoreEventType) -> None:
    state = MappingState("run-a")
    for index, status in enumerate(("running", "waiting", "pending", "done")):
        span = e.SpanStartedData.model_validate(
            {
                "span": {
                    "id": f"op:work-{index}",
                    "parent": None,
                    "type": "operation",
                    "name": "work",
                    "status": status,
                    "started_at": 0,
                    "ended_at": 0 if status == "done" else None,
                    "label": {"kind": "workstream", "attrs_json": "{}"},
                }
            }
        )
        state = map_event(state, event(e.CoreEventType.SPAN_STARTED, span)).state
    result = map_event(state, event(terminal))
    spans = [p.Span.from_binary(wire) for _, wire in result.state.spans]
    assert all(
        span.status in (p.SpanStatus.DONE, p.SpanStatus.FAILED, p.SpanStatus.STOPPED)
        for span in spans
    )
    assert all(span.has_field("ended_at") for span in spans)
    assert spans[-1].status == p.SpanStatus.DONE


@pytest.mark.regression
@example(text="ValueError: unsupported failure kind", stopped=True, already_stopped=True)
@given(
    text=st.text(alphabet="failure abc123:", max_size=40),
    stopped=st.booleans(),
    already_stopped=st.booleans(),
)
def test_unstructured_failure_overrides_the_provisional_ending(
    text: str, *, stopped: bool, already_stopped: bool
) -> None:
    state = MappingState("run-a")
    if stopped:
        state = map_event(
            state,
            event(
                e.CoreEventType.RUN_ENDING,
                e.RunEndingData(
                    reason=EndReason.STOPPED,
                    summary="stopped",
                    resumable=True,
                    rounds_finished=0,
                ),
            ),
        ).state
    if already_stopped:
        state = map_event(state, event(e.CoreEventType.STOPPED)).state
    failed = event(e.CoreEventType.RUN_FAILED).model_copy(update={"text": text})
    result = map_event(state, failed)
    lifecycle = next(r.body.value for r in result.records if r.body and r.body.field == "lifecycle")
    assert lifecycle.state is not None
    assert lifecycle.state.field == "ended"
    assert lifecycle.state.value.outcome == p.Outcome.FAILED
    assert lifecycle.state.value.failure is not None
    assert lifecycle.state.value.failure.reason == text


@pytest.mark.parametrize("kind", e.RunFailureKind)
def test_every_run_failure_kind_has_a_supported_native_reason(kind: e.RunFailureKind) -> None:
    failed = event(
        e.CoreEventType.RUN_FAILED,
        e.RunFailedData(
            failure=e.RunFailure(
                kind=kind,
                reason="failure",
                workstreams_started=0,
                workstream_budget=1,
                candidates_kept=0,
            )
        ),
    )
    result = map_event(MappingState("run-a"), failed)
    lifecycle = next(r.body.value for r in result.records if r.body and r.body.field == "lifecycle")
    assert lifecycle.state is not None
    assert lifecycle.state.field == "ended"
    assert lifecycle.state.value.failure is not None
    assert lifecycle.state.value.failure.kind != p.RunFailureKind.UNSPECIFIED


@pytest.mark.regression
@example(summary="stopped: kept the input", failed=False)
@given(summary=st.text(alphabet="summary abc123:", max_size=40), failed=st.booleans())
def test_late_ending_facts_update_the_head_without_downgrading_failure(
    summary: str, *, failed: bool
) -> None:
    state = map_event(
        MappingState("run-a"),
        event(
            e.CoreEventType.RUN_FAILED if failed else e.CoreEventType.STOPPED,
        ),
    ).state
    result = map_event(
        state,
        event(
            e.CoreEventType.RUN_ENDING,
            e.RunEndingData(
                reason=EndReason.STOPPED,
                summary=summary,
                resumable=True,
                rounds_finished=0,
            ),
        ),
    )
    lifecycle = next(r.body.value for r in result.records if r.body and r.body.field == "lifecycle")
    assert lifecycle.state is not None
    ending = lifecycle.state.value
    assert isinstance(ending, p.Ended)
    assert ending.outcome == (p.Outcome.FAILED if failed else p.Outcome.STOPPED)
    assert ending.summary == summary
    assert ending.resumable
