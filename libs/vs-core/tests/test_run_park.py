"""Park holds admission and preserves unfinished work for the next host epoch."""

from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurement_pacing import World, limits
from .test_measurements import plan
from .test_session_yield_completion import terminal_yield
from .test_silent_executor_deadline import BOUND, _silent
from .test_write_turn_retention import answer, snapshots, write_turn_world


def park(state: core.CoreState) -> core.Transition:
    return core.step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(control_id=core.ControlId(root="park"), action="park"),
            now_at=state.run.now_at,
        ),
    )


@given(st.integers(min_value=0, max_value=20))
def test_an_idle_park_is_terminal_for_the_epoch_and_idempotent(ticks: int) -> None:
    result = park(core.initial_state())
    assert result.state.run.status == core.RunStatus.PARKED
    assert result.state.run.result is None
    assert result.state.scheduling.admission_closed
    replay = park(result.state)
    assert replay.state.run == result.state.run
    assert not replay.requests
    for index in range(ticks):
        after = core.step(result.state, core.ClockAdvanced(now_at=float(index)))
        assert after.state == result.state
        assert not after.requests
    for control in (
        core.ControlInput(
            control_id=core.ControlId(root="other-pause"),
            action="pause",
            owner=core.PauseOwner.OPERATOR,
        ),
        core.ControlInput(
            control_id=core.ControlId(root="other-resume"), action="resume", when="now"
        ),
        core.ControlInput(control_id=core.ControlId(root="other-park"), action="park"),
    ):
        with pytest.raises(core.ControlRefusedError, match="epoch"):
            core.step(result.state, core.RunControlEvent(control=control, now_at=0))


@given(st.integers(min_value=1, max_value=20))
def test_park_cancels_each_job_once_and_keeps_its_measurement_pending(ticks: int) -> None:
    world = World(limits(1, 1))
    result = park(world.state)
    assert result.state.run.status == core.RunStatus.DRAINING
    cancels = [request for request in result.requests if isinstance(request, core.CancelOwnedJob)]
    assert len(cancels) == 1
    cancel = cancels[0]
    assert cancel.for_park
    assert result.state.evaluation.jobs == world.state.evaluation.jobs
    state = result.state
    for index in range(ticks):
        result = core.step(state, core.ClockAdvanced(now_at=float(index)))
        assert not any(isinstance(request, core.CancelOwnedJob) for request in result.requests)
        state = result.state
    assert state.run.status == core.RunStatus.DRAINING
    # Ordinary work can be prepared by a previously committed callback, but never dispatched.
    for record in state.records.records:
        if isinstance(record.request, core.ObserveOwnedJob):
            with pytest.raises(core.ContractError, match="park"):
                core.step(
                    state,
                    core.DispatchAuthorized(request_id=record.request_id, now_at=state.run.now_at),
                )

    assert cancel.request_id is not None
    state = core.step(
        state, core.DispatchAuthorized(request_id=cancel.request_id, now_at=state.run.now_at)
    ).state
    observation = core.Observation(
        event_id=core.EventId(root="park-cancelled"),
        request_id=cancel.request_id,
        scope=cancel.scope,
        admission_id=cancel.admission_id,
        sequence=1,
        observed_at=state.run.now_at,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        resource_id=cancel.resource_id,
    )
    completed = core.step(state, core.RequestObserved(observation=observation))
    assert completed.state.run.status == core.RunStatus.PARKED
    assert not completed.state.evaluation.jobs[0].terminal
    assert completed.state.evaluation.jobs[0].plan == world.job.plan
    assert not completed.requests


@given(access=st.sampled_from([core.Access.READ_ONLY, core.Access.WRITE_CANDIDATE]))
def test_a_turn_in_flight_finishes_and_its_revision_is_saved_before_park(
    access: core.Access,
) -> None:
    state, turn = write_turn_world(access=access)
    draining = park(state)
    assert draining.state.run.status == core.RunStatus.DRAINING
    assert not any(isinstance(request, core.CancelTurn) for request in draining.requests)
    ended = terminal_yield(draining.state, turn, None)
    state = core.step(draining.state, core.RequestObserved(observation=ended.observation)).state
    assert state.run.status == core.RunStatus.DRAINING
    completed = core.step(state, ended)
    checkpoints = snapshots(completed)
    assert len(checkpoints) == int(access == core.Access.WRITE_CANDIDATE)
    finished = completed
    if checkpoints:
        assert completed.state.run.status == core.RunStatus.DRAINING
        finished = answer(
            completed.state,
            checkpoints[0],
            core.ObservationStatus.SUCCEEDED,
            state.run.facts.baseline,
        )
    assert finished.state.run.status == core.RunStatus.PARKED
    assert finished.state.run.result is None


def test_a_prepared_turn_is_saved_without_starting_or_holding_the_drain() -> None:
    state, turn = write_turn_world()
    state = state.model_copy(
        update={
            "records": state.records.model_copy(
                update={
                    "records": tuple(
                        row.model_copy(update={"phase": core.RequestPhase.PREPARED})
                        if row.request_id == turn.request_id
                        else row
                        for row in state.records.records
                    )
                }
            )
        }
    )
    result = park(state)
    assert result.state.run.status == core.RunStatus.PARKED
    assert result.state.sessions == state.sessions
    assert not result.requests
    assert turn.request_id is not None
    with pytest.raises(core.ContractError, match="park"):
        core.step(result.state, core.DispatchAuthorized(request_id=turn.request_id))


@given(prepared=st.booleans())
def test_pause_drain_admits_only_an_executing_turns_evaluation_dependency(
    *, prepared: bool
) -> None:
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    if prepared:
        state = state.model_copy(
            update={
                "records": state.records.model_copy(
                    update={
                        "records": tuple(
                            row.model_copy(update={"phase": core.RequestPhase.PREPARED})
                            if row.request_id == turn.request_id
                            else row
                            for row in state.records.records
                        )
                    }
                )
            }
        )
    draining = park(state)
    result = core.step(
        draining.state,
        core.AgentMeasurementRequested(
            scope=turn.scope,
            plan=plan(),
            within=core.Within(limit=100.0),
            call_id="dependency",
        ),
    )
    submissions = [r for r in result.requests if isinstance(r, core.SubmitMeasurement)]
    if prepared:
        assert submissions == []
    else:
        assert len(submissions) == 1
        assert submissions[0].request_id is not None
        authorized = core.step(
            result.state, core.DispatchAuthorized(request_id=submissions[0].request_id)
        )
        assert authorized.state.run.status is core.RunStatus.DRAINING


@pytest.mark.regression
@example(epoch=1, immediate=False)
@given(epoch=st.integers(min_value=1, max_value=100), immediate=st.booleans())
def test_successor_resume_clears_an_interrupted_drain(epoch: int, *, immediate: bool) -> None:
    state, _ = write_turn_world(access=core.Access.READ_ONLY)
    state = state.model_copy(
        update={
            "records": state.records.model_copy(
                update={"recovery": state.records.recovery.model_copy(update={"epoch": epoch})}
            )
        }
    )
    paused = core.step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(
                control_id=core.ControlId(root="pause-old"),
                action="pause_now" if immediate else "pause",
                owner=None if immediate else core.PauseOwner.OPERATOR,
            ),
            now_at=state.run.now_at,
        ),
    ).state
    assert paused.run.status is core.RunStatus.DRAINING
    resume = core.RunControlEvent(
        control=core.ControlInput(
            control_id=core.ControlId(root="resume-new"), action="resume", when="now"
        ),
        now_at=paused.run.now_at,
    )
    with pytest.raises(core.ControlRefusedError, match="epoch"):
        core.step(paused, resume)
    opening = core.step(
        paused,
        core.EpochResumed(
            epoch=epoch + 1,
            now_at=paused.run.now_at,
        ),
    )
    successor = opening.state
    assert successor.run.status is core.RunStatus.RUNNING
    assert successor.run.park is None
    reopened = core.step(successor, resume)
    assert reopened.state.run.status is core.RunStatus.RUNNING
    assert reopened.state.run.park is None
    assert not reopened.state.scheduling.admission_closed
    assert any(
        isinstance(event, core.StopLiftRecorded) and event.new_epoch for event in opening.events
    )


_OPEN_RECORDS = [
    (core.RequestPhase.DISPATCHED, core.ObservationStatus.FAILED),
    (core.RequestPhase.RECONCILING, core.ObservationStatus.UNKNOWN),
]


def _pause(
    state: core.CoreState, *, action: Literal["pause", "pause_now"], drain_seconds: float | None
) -> core.CoreState:
    return core.step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(
                control_id=core.ControlId(root="pause"),
                action=action,
                owner=core.PauseOwner.OPERATOR if action == "pause" else None,
            ),
            now_at=state.run.now_at,
            drain_seconds=drain_seconds,
        ),
    ).state


@pytest.mark.regression
@example(
    open_record=_OPEN_RECORDS[0],
    lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
    action="pause_now",
    drain_seconds=5.0,
)
@given(
    open_record=st.sampled_from(_OPEN_RECORDS),
    lifecycle=st.sampled_from([core.LifecycleClass.IDEMPOTENT_WRITE, core.LifecycleClass.QUERY]),
    action=st.sampled_from(["pause", "pause_now"]),
    drain_seconds=st.floats(min_value=1.0, max_value=BOUND * 0.8),
)
def test_a_pause_past_its_deadline_interrupts_a_request_that_can_run_again_and_the_resume_sends_it(
    open_record: tuple[core.RequestPhase, core.ObservationStatus],
    lifecycle: core.LifecycleClass,
    action: Literal["pause", "pause_now"],
    drain_seconds: float,
) -> None:
    """The park never leaves the request open, and it is no failure, no retry, no report.

    Regression (#3475): a Pause-now exited with the session start of the planner in flight, so the
    Resume had to prove what became of it and ended the run blocked.
    """
    phase, status = open_record
    state = _silent(phase, status, terminal=False)
    [row] = state.records.records
    state = state.model_copy(
        update={
            "records": state.records.model_copy(
                update={"records": (row.model_copy(update={"lifecycle": lifecycle}),)}
            )
        }
    )
    paused = _pause(state, action=action, drain_seconds=drain_seconds)
    assert paused.run.status is core.RunStatus.DRAINING
    assert paused.run.park is not None
    deadline = paused.run.park.deadline_at
    assert deadline == paused.run.now_at + drain_seconds
    assert deadline is not None
    waiting = core.step(paused, core.ClockAdvanced(now_at=deadline - 0.5))
    assert waiting.state.run.status is core.RunStatus.DRAINING
    [still] = waiting.state.records.records
    assert still.phase is phase
    interrupted = core.step(waiting.state, core.ClockAdvanced(now_at=deadline))
    assert interrupted.state.run.status is core.RunStatus.PARKED
    [sent_back] = interrupted.state.records.records
    assert sent_back.phase is core.RequestPhase.PREPARED
    assert sent_back.observation is None
    assert sent_back.retry_count == row.retry_count
    assert sent_back.request == row.request
    assert not interrupted.requests
    assert not any(isinstance(event, core.Failed) for event in interrupted.events)
    # The next epoch dispatches the same request again.
    epoch = interrupted.state.records.recovery.epoch + 1
    resumed = core.step(
        interrupted.state, core.EpochResumed(epoch=epoch, now_at=deadline + 1)
    ).state
    assert resumed.run.status is core.RunStatus.RUNNING
    request_id = sent_back.request_id
    while (record := resumed.records.record(request_id)) is not None and (
        record.phase is core.RequestPhase.PREPARED
        and resumed.records.recovery.phase is not core.RecoveryPhase.READY
    ):
        resumed = core.step(resumed, core.ClockAdvanced(now_at=resumed.run.now_at + 1)).state
    authorized = core.step(resumed, core.DispatchAuthorized(request_id=request_id)).state
    dispatched = authorized.records.record(request_id)
    assert dispatched is not None
    assert dispatched.phase is core.RequestPhase.DISPATCHED
    assert dispatched.retry_count == row.retry_count


@given(
    lifecycle=st.sampled_from([core.LifecycleClass.OWNED_JOB, core.LifecycleClass.SESSION_TURN]),
    drain_seconds=st.floats(min_value=1.0, max_value=BOUND * 0.4),
)
def test_a_request_that_cannot_run_again_ends_a_cleanup_bound_after_the_deadline_not_before(
    lifecycle: core.LifecycleClass, drain_seconds: float
) -> None:
    state = _silent(core.RequestPhase.DISPATCHED, core.ObservationStatus.FAILED, terminal=False)
    [row] = state.records.records
    state = state.model_copy(
        update={
            "records": state.records.model_copy(
                update={
                    "records": (
                        row.model_copy(
                            update={
                                "lifecycle": lifecycle,
                                "until": row.until.model_copy(update={"at": 1e9}),
                            }
                        ),
                    )
                }
            )
        }
    )
    paused = _pause(state, action="pause_now", drain_seconds=drain_seconds)
    assert paused.run.park is not None
    assert paused.run.park.deadline_at is not None
    bound = paused.run.limits.cleanup_bound
    due = paused.run.park.deadline_at + bound
    [capped] = paused.records.records
    assert capped.until.at == due
    at_deadline = core.step(paused, core.ClockAdvanced(now_at=due - 0.5)).state
    assert at_deadline.run.status is core.RunStatus.DRAINING
    [open_row] = at_deadline.records.records
    assert open_row.phase is core.RequestPhase.DISPATCHED
    ended = core.step(at_deadline, core.ClockAdvanced(now_at=due)).state
    blocked = ended.records.record(row.request_id)
    assert blocked is not None
    assert blocked.phase is core.RequestPhase.BLOCKED


@given(
    action=st.sampled_from(["pause", "pause_now"]),
    drain_seconds=st.one_of(st.none(), st.floats(min_value=0.5, max_value=1e4)),
)
def test_the_drain_deadline_is_the_pause_inputs_own_field(
    action: Literal["pause", "pause_now"], drain_seconds: float | None
) -> None:
    """A Pause-now that names none waits the cleanup bound; a Pause that names none, for its turns."""
    state = core.initial_state()
    paused = _pause(state, action=action, drain_seconds=drain_seconds)
    assert paused.run.park is not None
    expected = (
        paused.run.now_at + drain_seconds
        if drain_seconds is not None
        else paused.run.now_at + paused.run.limits.cleanup_bound
        if action == "pause_now"
        else None
    )
    assert paused.run.park.deadline_at == expected
