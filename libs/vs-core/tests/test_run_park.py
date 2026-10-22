"""Park holds admission and preserves unfinished work for the next host epoch."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurement_pacing import World, limits
from .test_measurements import plan
from .test_session_yield_completion import terminal_yield
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
        core.RecoveryStarted(
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
