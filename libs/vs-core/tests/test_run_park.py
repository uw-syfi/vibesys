"""Park holds admission and preserves unfinished work for the next host epoch."""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import reach

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
    ended = reach(at_deadline, due).state
    done = ended.records.record(row.request_id)
    assert done is not None
    assert done.phase not in (core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING)


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
