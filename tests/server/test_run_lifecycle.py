"""Exhaustive rules of the run lifecycle state machine."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from server.host_api import FailureReason, RunPhase, StartupFact, StartupProgress, StartupStage
from server.run_lifecycle import (
    IllegalRunTransitionError,
    RunRequest,
    RunState,
    RunStatus,
    RunTrigger,
    transition,
)

_STARTING_STATE = RunState(RunStatus.STARTING)
_HOLDING_PAUSE = RunState(RunStatus.STARTING, RunRequest.PAUSE)
_HOLDING_STOP = RunState(RunStatus.STARTING, RunRequest.STOP)
_STARTING_STATES = (_STARTING_STATE, _HOLDING_PAUSE, _HOLDING_STOP)

_READY_MOVES: dict[tuple[RunStatus, RunTrigger], RunStatus] = {
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
    (RunStatus.STOPPING, RunTrigger.RESUMED): RunStatus.STOPPING,
    (RunStatus.STOPPING, RunTrigger.STOP_REQUESTED): RunStatus.STOPPING,
    (RunStatus.STOPPING, RunTrigger.COMPLETED): RunStatus.COMPLETED,
    (RunStatus.STOPPING, RunTrigger.FAILED): RunStatus.FAILED,
}
"""The moves past ``starting``, where a state is its status alone."""

LEGAL: dict[tuple[RunState, RunTrigger], RunState] = {
    (_STARTING_STATE, RunTrigger.READY): RunState(RunStatus.RUNNING),
    (_STARTING_STATE, RunTrigger.PAUSE_REQUESTED): _HOLDING_PAUSE,
    (_STARTING_STATE, RunTrigger.STOP_REQUESTED): _HOLDING_STOP,
    (_STARTING_STATE, RunTrigger.RESUMED): _STARTING_STATE,
    (_STARTING_STATE, RunTrigger.COMPLETED): RunState(RunStatus.COMPLETED),
    (_STARTING_STATE, RunTrigger.FAILED): RunState(RunStatus.FAILED),
    (_HOLDING_PAUSE, RunTrigger.READY): RunState(RunStatus.PAUSING),
    (_HOLDING_PAUSE, RunTrigger.PAUSE_REQUESTED): _HOLDING_PAUSE,
    (_HOLDING_PAUSE, RunTrigger.STOP_REQUESTED): _HOLDING_STOP,
    (_HOLDING_PAUSE, RunTrigger.RESUMED): _STARTING_STATE,
    (_HOLDING_PAUSE, RunTrigger.COMPLETED): RunState(RunStatus.COMPLETED),
    (_HOLDING_PAUSE, RunTrigger.FAILED): RunState(RunStatus.FAILED),
    (_HOLDING_STOP, RunTrigger.READY): RunState(RunStatus.STOPPING),
    (_HOLDING_STOP, RunTrigger.INVOCATION_FINISHED): RunState(RunStatus.STOPPED),
    (_HOLDING_STOP, RunTrigger.PAUSE_REQUESTED): _HOLDING_STOP,
    (_HOLDING_STOP, RunTrigger.STOP_REQUESTED): _HOLDING_STOP,
    (_HOLDING_STOP, RunTrigger.RESUMED): _HOLDING_STOP,
    (_HOLDING_STOP, RunTrigger.COMPLETED): RunState(RunStatus.COMPLETED),
    (_HOLDING_STOP, RunTrigger.FAILED): RunState(RunStatus.FAILED),
    **{
        (RunState(current), trigger): RunState(after)
        for (current, trigger), after in _READY_MOVES.items()
    },
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
LIVE_STATES = (*_STARTING_STATES, *(RunState(status) for status in LIVE[1:]))

ILLEGAL = [
    (current, trigger)
    for current in LIVE_STATES
    for trigger in RunTrigger
    if (current, trigger) not in LEGAL
]


def test_every_status_is_either_ended_or_live() -> None:
    """``has_ended`` partitions the enum, so a new member has to be placed."""
    assert set(ENDED) | set(LIVE) == set(RunStatus)
    assert not set(ENDED) & set(LIVE)
    assert [status for status in RunStatus if status.has_ended] == list(ENDED)
    assert [status for status in RunStatus if not status.has_ended] == list(LIVE)


@pytest.mark.parametrize(
    "status", [status for status in RunStatus if status is not RunStatus.STARTING], ids=str
)
@pytest.mark.parametrize("requested", list(RunRequest), ids=str)
def test_only_a_starting_run_holds_a_request(status: RunStatus, requested: RunRequest) -> None:
    with pytest.raises(ValueError, match="only a starting run holds a request"):
        RunState(status, requested)


@pytest.mark.parametrize(("pair", "expected"), LEGAL.items(), ids=str)
def test_legal_transition(pair: tuple[RunState, RunTrigger], expected: RunState) -> None:
    """Every legal move produces the state the table names."""
    current, trigger = pair
    assert transition(current, trigger) == expected


@pytest.mark.parametrize(("current", "trigger"), ILLEGAL, ids=str)
def test_illegal_transition_raises(current: RunState, trigger: RunTrigger) -> None:
    """Every move outside the table is rejected with the offending pair."""
    with pytest.raises(IllegalRunTransitionError) as raised:
        transition(current, trigger)
    assert raised.value.current is current.status
    assert raised.value.trigger is trigger


def test_illegal_pairs_are_the_ones_we_expect() -> None:
    """No invocation can finish before the run is ready, except where a held stop lands.

    Pinned so widening the table stays a deliberate edit.
    """
    assert set(ILLEGAL) == {
        (_STARTING_STATE, RunTrigger.INVOCATION_FINISHED),
        (_HOLDING_PAUSE, RunTrigger.INVOCATION_FINISHED),
    }


@pytest.mark.parametrize("state", LIVE_STATES, ids=str)
def test_a_pending_pause_or_stop_is_one_held_or_entered(state: RunState) -> None:
    assert state.pause_pending == (
        state.status is RunStatus.PAUSING or state.requested is RunRequest.PAUSE
    )
    assert state.stop_pending == (
        state.status is RunStatus.STOPPING or state.requested is RunRequest.STOP
    )


def test_a_stop_held_while_starting_can_land_before_ready() -> None:
    """Core may abandon a startup at a held stop; the run then ends ``stopped``."""
    status = _status_after(
        RunStatus.STARTING, RunTrigger.STOP_REQUESTED, RunTrigger.INVOCATION_FINISHED
    )
    assert status is RunStatus.STOPPED
    assert _status_after(status, RunTrigger.READY) is RunStatus.STOPPED


@pytest.mark.parametrize("ended", ENDED, ids=str)
@pytest.mark.parametrize("trigger", list(RunTrigger), ids=str)
def test_ended_statuses_absorb_every_trigger(ended: RunStatus, trigger: RunTrigger) -> None:
    """A run that has ended never leaves the status it ended in."""
    assert transition(RunState(ended), trigger) == RunState(ended)


def _status_after(status: RunStatus, *triggers: RunTrigger) -> RunStatus:
    state = RunState(status)
    for trigger in triggers:
        state = transition(state, trigger)
    return state.status


def test_pause_reaches_its_boundary_then_resumes() -> None:
    """The canonical pause round trip, as the controller drives it."""
    status = _status_after(RunStatus.STARTING, RunTrigger.READY)
    assert status is RunStatus.RUNNING
    status = _status_after(status, RunTrigger.PAUSE_REQUESTED)
    assert status is RunStatus.PAUSING
    status = _status_after(status, RunTrigger.INVOCATION_FINISHED)
    assert status is RunStatus.PAUSED
    status = _status_after(status, RunTrigger.RESUMED)
    assert status is RunStatus.RUNNING


def test_resume_before_the_boundary_cancels_the_pending_pause() -> None:
    """A ``/resume`` that beats the boundary leaves nothing pending."""
    status = _status_after(RunStatus.RUNNING, RunTrigger.PAUSE_REQUESTED, RunTrigger.RESUMED)
    assert status is RunStatus.RUNNING
    assert _status_after(status, RunTrigger.INVOCATION_FINISHED) is RunStatus.RUNNING


def test_stop_reaches_its_boundary_and_never_leaves() -> None:
    """The canonical stop: request, land at the boundary, stay ended."""
    status = _status_after(RunStatus.RUNNING, RunTrigger.STOP_REQUESTED)
    assert status is RunStatus.STOPPING
    status = _status_after(status, RunTrigger.INVOCATION_FINISHED)
    assert status is RunStatus.STOPPED
    assert _status_after(status, RunTrigger.RESUMED) is RunStatus.STOPPED


@pytest.mark.parametrize("origin", [RunStatus.RUNNING, RunStatus.PAUSING, RunStatus.PAUSED])
def test_stop_is_requestable_wherever_the_run_is_live(origin: RunStatus) -> None:
    """A stop supersedes a pause pending or landed at the same boundary."""
    status = _status_after(origin, RunTrigger.STOP_REQUESTED)
    assert status is RunStatus.STOPPING
    assert _status_after(status, RunTrigger.INVOCATION_FINISHED) is RunStatus.STOPPED


def test_a_resume_never_cancels_a_pending_stop() -> None:
    """A stop only escalates: a ``/resume`` before the stop's boundary leaves it pending."""
    status = _status_after(RunStatus.RUNNING, RunTrigger.STOP_REQUESTED, RunTrigger.RESUMED)
    assert status is RunStatus.STOPPING
    assert _status_after(status, RunTrigger.INVOCATION_FINISHED) is RunStatus.STOPPED


def test_a_pause_or_stop_requested_while_starting_lands_after_ready() -> None:
    """A control requested during startup is held; readiness does not cancel it."""
    for request, pending, landed in (
        (RunTrigger.PAUSE_REQUESTED, RunStatus.PAUSING, RunStatus.PAUSED),
        (RunTrigger.STOP_REQUESTED, RunStatus.STOPPING, RunStatus.STOPPED),
    ):
        assert _status_after(RunStatus.STARTING, request) is RunStatus.STARTING
        status = _status_after(RunStatus.STARTING, request, RunTrigger.READY)
        assert status is pending
        assert _status_after(status, RunTrigger.INVOCATION_FINISHED) is landed


def test_a_pause_resumed_before_ready_keeps_the_run_starting() -> None:
    """Regression: a resume during startup used to report ``running`` before readiness."""
    status = _status_after(RunStatus.STARTING, RunTrigger.PAUSE_REQUESTED, RunTrigger.RESUMED)
    assert status is RunStatus.STARTING
    assert _status_after(status, RunTrigger.READY) is RunStatus.RUNNING


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


_CONTROL = st.sampled_from(
    [RunTrigger.PAUSE_REQUESTED, RunTrigger.RESUMED, RunTrigger.STOP_REQUESTED]
)
_LANDS_AS = {
    None: RunStatus.RUNNING,
    RunTrigger.PAUSE_REQUESTED: RunStatus.PAUSING,
    RunTrigger.STOP_REQUESTED: RunStatus.STOPPING,
}


class _Fold:
    """The controller's state and the record's phase, folded as production folds them.

    The phase sees a status only when the controller publishes a change, as
    ``entrypoints.detached_run`` does through the journal.
    """

    def __init__(self) -> None:
        self.state = RunState(RunStatus.STARTING)
        self.phase = _STARTING
        self.request: RunTrigger | None = None

    def apply(self, item: StartupFact | RunTrigger) -> None:
        if isinstance(item, StartupFact):
            self.phase = self.phase.advance(startup=item)
            return
        previous = self.state.status
        self.state = transition(self.state, item)
        if self.state.status is not previous:
            self.phase = self.phase.advance(status=self.state.status)
        if item is RunTrigger.RESUMED:
            # A resume cancels a pending pause, never a stop.
            if self.request is not RunTrigger.STOP_REQUESTED:
                self.request = None
        elif item is RunTrigger.STOP_REQUESTED or self.request is None:
            self.request = item


@given(
    before=st.lists(st.one_of(_STARTUP, _CONTROL), max_size=16),
    after=st.lists(st.one_of(_STARTUP, _CONTROL), max_size=6),
)
def test_startup_reports_starting_with_monotone_progress_until_ready(
    before: list[StartupFact | RunTrigger], after: list[StartupFact | RunTrigger]
) -> None:
    """Across any interleaving of startup facts and controls, a run is ``starting`` until ready.

    Before ``READY`` the status stays ``starting``, its stage never moves back,
    and a control leaves stage and progress as they were; ``READY`` lands the
    request still held, and the run never reports ``starting`` again.
    """
    fold = _Fold()
    for item in before:
        stage, progress = fold.phase.stage, fold.phase.progress
        fold.apply(item)
        assert fold.state.status is RunStatus.STARTING
        assert fold.phase.status is RunStatus.STARTING
        assert fold.phase.stage is not None
        assert stage is not None
        assert fold.phase.stage.rank >= stage.rank
        if isinstance(item, RunTrigger):
            assert (fold.phase.stage, fold.phase.progress) == (stage, progress)
        elif item.stage.rank >= stage.rank:
            assert (fold.phase.stage, fold.phase.progress) == (item.stage, item.progress)

    expected = _LANDS_AS[fold.request]
    fold.apply(RunTrigger.READY)
    assert fold.state.status is expected
    assert fold.phase == RunPhase(status=expected)
    for item in after:
        fold.apply(item)
        assert fold.state.status is not RunStatus.STARTING
        assert fold.phase.status is not RunStatus.STARTING


@given(before=st.lists(st.one_of(_STARTUP, _CONTROL), max_size=16))
def test_core_landing_a_held_stop_before_ready_ends_the_run_stopped(
    before: list[StartupFact | RunTrigger],
) -> None:
    """Core lands a stop while starting only if one is held; the run then ends ``stopped``.

    The controller fires the landing only while a stop is pending, as
    ``RunController.land_stop_at_boundary`` does.
    """
    fold = _Fold()
    for item in before:
        fold.apply(item)
    held = fold.state.stop_pending
    assert held == (fold.request is RunTrigger.STOP_REQUESTED)
    if held:
        fold.apply(RunTrigger.INVOCATION_FINISHED)
        assert fold.state == RunState(RunStatus.STOPPED)
        assert fold.phase == RunPhase(status=RunStatus.STOPPED)
    expected = RunStatus.STOPPED if held else _LANDS_AS[fold.request]
    fold.apply(RunTrigger.READY)
    assert fold.state.status is expected
