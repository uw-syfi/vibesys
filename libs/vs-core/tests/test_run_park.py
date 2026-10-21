"""Park holds admission and preserves unfinished work for the next host epoch."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurement_pacing import World, limits
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
    for action, fields in (
        ("pause", {"owner": core.PauseOwner.OPERATOR}),
        ("resume", {"when": "now"}),
        ("park", {}),
    ):
        control = core.ControlInput(
            control_id=core.ControlId(root=f"other-{action}"), action=action, **fields
        )
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


def test_a_turn_in_flight_finishes_and_its_revision_is_saved_before_park() -> None:
    state, turn = write_turn_world()
    draining = park(state)
    assert draining.state.run.status == core.RunStatus.DRAINING
    assert not any(isinstance(request, core.CancelTurn) for request in draining.requests)
    ended = terminal_yield(draining.state, turn, None)
    state = core.step(draining.state, core.RequestObserved(observation=ended.observation)).state
    assert state.run.status == core.RunStatus.DRAINING
    completed = core.step(state, ended)
    [snapshot] = snapshots(completed)
    assert completed.state.run.status == core.RunStatus.DRAINING
    finished = answer(
        completed.state, snapshot, core.ObservationStatus.SUCCEEDED, state.run.facts.baseline
    )
    assert finished.state.run.status == core.RunStatus.PARKED
    assert finished.state.run.result is None
