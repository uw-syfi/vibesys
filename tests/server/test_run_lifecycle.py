"""Exhaustive rules of the run lifecycle state machine."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from server.host_api import FailureReason, RunPhase, StartupFact, StartupProgress, StartupStage
from server.run_lifecycle import (
    IllegalRunTransitionError,
    RunStatus,
    RunTrigger,
    transition,
)

LEGAL: dict[tuple[RunStatus, RunTrigger], RunStatus] = {
    (RunStatus.STARTING, RunTrigger.READY): RunStatus.RUNNING,
    (RunStatus.STARTING, RunTrigger.PAUSE_REQUESTED): RunStatus.PAUSING,
    (RunStatus.STARTING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
    (RunStatus.STARTING, RunTrigger.RESUMED): RunStatus.STARTING,
    (RunStatus.STARTING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
    (RunStatus.STARTING, RunTrigger.FAILED): RunStatus.FAILED,
    (RunStatus.RUNNING, RunTrigger.READY): RunStatus.RUNNING,
    (RunStatus.RUNNING, RunTrigger.PAUSE_REQUESTED): RunStatus.PAUSING,
    (RunStatus.RUNNING, RunTrigger.INVOCATION_FINISHED): RunStatus.RUNNING,
    (RunStatus.RUNNING, RunTrigger.RESUMED): RunStatus.RUNNING,
    (RunStatus.RUNNING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
    (RunStatus.RUNNING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
    (RunStatus.RUNNING, RunTrigger.FAILED): RunStatus.FAILED,
    (RunStatus.PAUSING, RunTrigger.READY): RunStatus.PAUSING,
    (RunStatus.PAUSING, RunTrigger.PAUSE_REQUESTED): RunStatus.PAUSING,
    (RunStatus.PAUSING, RunTrigger.INVOCATION_FINISHED): RunStatus.PAUSED,
    (RunStatus.PAUSING, RunTrigger.RESUMED): RunStatus.RUNNING,
    (RunStatus.PAUSING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
    (RunStatus.PAUSING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
    (RunStatus.PAUSING, RunTrigger.FAILED): RunStatus.FAILED,
    (RunStatus.PAUSED, RunTrigger.READY): RunStatus.PAUSED,
    (RunStatus.PAUSED, RunTrigger.PAUSE_REQUESTED): RunStatus.PAUSED,
    (RunStatus.PAUSED, RunTrigger.INVOCATION_FINISHED): RunStatus.PAUSED,
    (RunStatus.PAUSED, RunTrigger.RESUMED): RunStatus.RUNNING,
    (RunStatus.PAUSED, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
    (RunStatus.PAUSED, RunTrigger.COMPLETED): RunStatus.COMPLETED,
    (RunStatus.PAUSED, RunTrigger.FAILED): RunStatus.FAILED,
    (RunStatus.STOPPING, RunTrigger.READY): RunStatus.STOPPING,
    (RunStatus.STOPPING, RunTrigger.PAUSE_REQUESTED): RunStatus.STOPPING,
    (RunStatus.STOPPING, RunTrigger.INVOCATION_FINISHED): RunStatus.STOPPED,
    (RunStatus.STOPPING, RunTrigger.RESUMED): RunStatus.RUNNING,
    (RunStatus.STOPPING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
    (RunStatus.STOPPING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
    (RunStatus.STOPPING, RunTrigger.FAILED): RunStatus.FAILED,
}
"""The whole table, restated independently of the module under test."""

ENDED = (RunStatus.STOPPED, RunStatus.COMPLETED, RunStatus.FAILED)
LIVE = (
    RunStatus.STARTING,
    RunStatus.RUNNING,
    RunStatus.PAUSING,
    RunStatus.PAUSED,
    RunStatus.STOPPING,
)

ILLEGAL = [
    (current, trigger)
    for current in LIVE
    for trigger in RunTrigger
    if (current, trigger) not in LEGAL
]


def test_every_status_is_either_ended_or_live() -> None:
    """``has_ended`` partitions the enum, so a new member has to be placed."""
    assert set(ENDED) | set(LIVE) == set(RunStatus)
    assert not set(ENDED) & set(LIVE)
    assert [status for status in RunStatus if status.has_ended] == list(ENDED)
    assert [status for status in RunStatus if not status.has_ended] == list(LIVE)


@pytest.mark.parametrize(("pair", "expected"), LEGAL.items(), ids=str)
def test_legal_transition(pair: tuple[RunStatus, RunTrigger], expected: RunStatus) -> None:
    """Every legal move produces the status the table names."""
    current, trigger = pair
    assert transition(current, trigger) is expected


@pytest.mark.parametrize(("current", "trigger"), ILLEGAL, ids=str)
def test_illegal_transition_raises(current: RunStatus, trigger: RunTrigger) -> None:
    """Every move outside the table is rejected with the offending pair."""
    with pytest.raises(IllegalRunTransitionError) as raised:
        transition(current, trigger)
    assert raised.value.current is current
    assert raised.value.trigger is trigger


def test_illegal_pairs_are_the_ones_we_expect() -> None:
    """No invocation can finish before the run is ready.

    Pinned so widening the table stays a deliberate edit.
    """
    assert set(ILLEGAL) == {(RunStatus.STARTING, RunTrigger.INVOCATION_FINISHED)}


@pytest.mark.parametrize("ended", ENDED, ids=str)
@pytest.mark.parametrize("trigger", list(RunTrigger), ids=str)
def test_ended_statuses_absorb_every_trigger(ended: RunStatus, trigger: RunTrigger) -> None:
    """A run that has ended never leaves the status it ended in."""
    assert transition(ended, trigger) is ended


def test_pause_reaches_its_boundary_then_resumes() -> None:
    """The canonical pause round trip, as the controller drives it."""
    status = transition(RunStatus.STARTING, RunTrigger.READY)
    assert status is RunStatus.RUNNING
    status = transition(status, RunTrigger.PAUSE_REQUESTED)
    assert status is RunStatus.PAUSING
    status = transition(status, RunTrigger.INVOCATION_FINISHED)
    assert status is RunStatus.PAUSED
    status = transition(status, RunTrigger.RESUMED)
    assert status is RunStatus.RUNNING


def test_resume_before_the_boundary_cancels_the_pending_pause() -> None:
    """A ``/resume`` that beats the boundary leaves nothing pending."""
    status = transition(RunStatus.RUNNING, RunTrigger.PAUSE_REQUESTED)
    status = transition(status, RunTrigger.RESUMED)
    assert status is RunStatus.RUNNING
    assert transition(status, RunTrigger.INVOCATION_FINISHED) is RunStatus.RUNNING


def test_stop_reaches_its_boundary_and_never_leaves() -> None:
    """The canonical stop: request, land at the boundary, stay ended."""
    status = transition(RunStatus.RUNNING, RunTrigger.STOP_REQUESTED)
    assert status is RunStatus.STOPPING
    status = transition(status, RunTrigger.INVOCATION_FINISHED)
    assert status is RunStatus.STOPPED
    assert transition(status, RunTrigger.RESUMED) is RunStatus.STOPPED


@pytest.mark.parametrize("origin", [RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.PAUSED])
def test_stop_is_requestable_wherever_the_run_is_live(origin: RunStatus) -> None:
    """A stop supersedes a pause pending or landed at the same boundary."""
    status = transition(origin, RunTrigger.STOP_REQUESTED)
    assert status is RunStatus.STOPPING
    assert transition(status, RunTrigger.INVOCATION_FINISHED) is RunStatus.STOPPED


def test_resume_before_the_boundary_cancels_the_pending_stop() -> None:
    """A ``/resume`` that beats the stop boundary keeps the run going."""
    status = transition(RunStatus.RUNNING, RunTrigger.STOP_REQUESTED)
    status = transition(status, RunTrigger.RESUMED)
    assert status is RunStatus.RUNNING
    assert transition(status, RunTrigger.INVOCATION_FINISHED) is RunStatus.RUNNING


def test_a_pause_or_stop_requested_while_starting_lands_after_ready() -> None:
    """A control requested during startup is pending; readiness does not cancel it."""
    for request, pending, landed in (
        (RunTrigger.PAUSE_REQUESTED, RunStatus.PAUSING, RunStatus.PAUSED),
        (RunTrigger.STOP_REQUESTED, RunStatus.STOPPING, RunStatus.STOPPED),
    ):
        status = transition(RunStatus.STARTING, request)
        assert status is pending
        status = transition(status, RunTrigger.READY)
        assert status is pending
        assert transition(status, RunTrigger.INVOCATION_FINISHED) is landed


_REASON = FailureReason(code="boom", stage="run", message="boom")
_PROGRESS = st.one_of(
    st.none(),
    st.builds(
        StartupProgress,
        done=st.integers(0, 20),
        total=st.one_of(st.none(), st.integers(21, 30)),
        label=st.text(max_size=8),
    ),
)
_STARTUP = st.builds(StartupFact, stage=st.sampled_from(StartupStage), progress=_PROGRESS)
_FACTS = st.one_of(
    st.builds(dict, status=st.sampled_from(RunStatus)),
    st.builds(dict, failure=st.just(_REASON)),
    st.builds(dict, startup=_STARTUP),
)
_STARTING = RunPhase(status=RunStatus.STARTING, stage=StartupStage.RESOLVING)


@given(facts=st.lists(_FACTS, max_size=12))
def test_a_phase_only_moves_forward_and_names_why_it_failed(facts: list[dict]) -> None:
    phase = _STARTING
    for fact in facts:
        after = phase.advance(**fact)
        if phase.status.has_ended:
            assert after == phase
        if after.status is RunStatus.STARTING:
            assert phase.status is RunStatus.STARTING
            assert phase.stage is not None
            assert after.stage is not None
            assert after.stage.rank >= phase.stage.rank
        if after.status is RunStatus.FAILED and not phase.status.has_ended:
            expected = (
                _REASON
                if phase.stage in {None, StartupStage.RESOLVING}
                else _REASON.model_copy(update={"stage": phase.stage})
            )
            assert after.reason == expected
        phase = after


@given(stages=st.lists(_STARTUP, max_size=12), ending=st.sampled_from(["ready", "failed"]))
def test_startup_stages_are_monotone_and_end_ready_or_failed_with_a_reason(
    stages: list[StartupFact], ending: str
) -> None:
    phase = _STARTING
    seen = [phase.stage]
    for fact in stages:
        phase = phase.advance(startup=fact)
        seen.append(phase.stage)
    assert [stage.rank for stage in seen if stage is not None] == sorted(
        stage.rank for stage in seen if stage is not None
    )
    last = phase.stage
    assert last is not None
    if ending == "ready":
        assert phase.advance(status=RunStatus.RUNNING) == RunPhase(status=RunStatus.RUNNING)
    else:
        failed = phase.advance(failure=_REASON)
        assert failed.status is RunStatus.FAILED
        assert failed.reason is not None
        assert failed.reason.stage == (
            _REASON.stage if last is StartupStage.RESOLVING else last.value
        )


def test_a_build_failure_names_the_building_image_stage() -> None:
    building = _STARTING.advance(
        startup=StartupFact(
            stage=StartupStage.BUILDING_IMAGE,
            progress=StartupProgress(done=3, total=14, label="RUN npm install"),
        )
    )
    reason = FailureReason(
        code="run_failed", stage="run", message="Could not build agent image (exit 1)"
    )

    assert building.advance(failure=reason) == RunPhase(
        status=RunStatus.FAILED,
        reason=FailureReason(
            code="run_failed",
            stage="building_image",
            message="Could not build agent image (exit 1)",
        ),
    )


def test_a_failed_status_waits_for_the_failure_that_says_why() -> None:
    running = RunPhase(status=RunStatus.RUNNING)

    assert running.advance(status=RunStatus.FAILED) == running
    assert running.advance(failure=_REASON) == RunPhase(status=RunStatus.FAILED, reason=_REASON)
