"""A logical turn's permit is the only admission of its successors, and their deadline."""

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_agent.api import AgentExecutionCancelledError
from vs_runtime.api.infrastructure import create_run_control_channel
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads, examples

from .test_measurements import plan
from .test_run_park import park
from .test_write_turn_retention import write_turn_world

_DEADLINE = 100.0


def _at(state: core.CoreState, now_at: float) -> core.CoreState:
    if now_at == state.run.now_at:
        return state
    return core.step(state, core.ClockAdvanced(now_at=now_at)).state


def _deadline(state: core.CoreState, request_id: core.RequestId) -> float:
    record = state.records.record(request_id)
    assert record is not None
    return record.until.at


@pytest.mark.regression
def test_a_drain_evaluation_keeps_the_turns_deadline() -> None:
    """Pause at 99 of a turn due at 100 must not accept an evaluation due at 199."""
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    state = _at(state, 99.0)
    draining = park(state)
    result = core.step(
        draining.state,
        core.AgentMeasurementRequested(
            scope=turn.scope,
            plan=plan(),
            within=core.Within(limit=100.0),
            call_id="late",
        ),
    )
    submissions = [row for row in result.requests if isinstance(row, core.SubmitMeasurement)]
    assert len(submissions) == 1
    request_id = submissions[0].request_id
    assert request_id is not None
    assert _deadline(result.state, request_id) <= _DEADLINE
    answered = result.state.evaluation.agent_calls[-1]
    assert answered.deadline_at is not None
    assert answered.deadline_at <= _DEADLINE


@pytest.mark.regression
def test_an_evaluation_queued_before_pause_keeps_the_turns_deadline() -> None:
    """A submission requested while the turn is running cannot outlive that turn."""
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    wide = plan(queue_allowance=400.0)
    result = core.step(
        state,
        core.AgentMeasurementRequested(
            scope=turn.scope,
            plan=wide,
            within=core.Within(limit=400.0),
            call_id="queued",
        ),
    )
    submissions = [row for row in result.requests if isinstance(row, core.SubmitMeasurement)]
    assert len(submissions) == 1
    request_id = submissions[0].request_id
    assert request_id is not None
    assert _deadline(result.state, request_id) <= _DEADLINE
    paused = park(result.state)
    assert _deadline(paused.state, request_id) <= _DEADLINE


@pytest.mark.regression
@example(pause_at=99, limits=(100,), continuations=1)
@example(pause_at=100, limits=(100,), continuations=1)
@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
@given(
    pause_at=st.integers(min_value=0, max_value=100),
    limits=st.lists(st.integers(min_value=1, max_value=400), max_size=3),
    continuations=st.integers(min_value=0, max_value=3),
)
def test_admitted_successors_start_inside_the_turn_deadline(
    pause_at: int, limits: list[int], continuations: int
) -> None:
    """Pause at any point, then any continuations and evaluations.

    Every successor core admits starts, and none of that work is due after the turn.
    """
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    state = _at(state, float(pause_at))
    draining = park(state).state
    permit = core.permit_for_request(draining, turn)
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    channel.land_park()
    if permit is None:
        channel.prepare_execution("blocked", turn.scope)
        with pytest.raises(AgentExecutionCancelledError):
            channel.start_execution(lambda: None, execution_id="blocked")
    else:
        assert permit.deadline_at == _DEADLINE
        for index in range(continuations):
            execution_id = f"continuation-{index}"
            channel.prepare_execution(execution_id, turn.scope, permit=permit)
            started: list[str] = []
            channel.start_execution(
                lambda execution_id=execution_id, started=started: started.append(execution_id),
                execution_id=execution_id,
            )
            assert started == [execution_id]
            channel.finish_execution(execution_id)
    for index, limit in enumerate(limits):
        call_id = f"call-{pause_at}-{index}"
        result = core.step(
            draining,
            core.AgentMeasurementRequested(
                scope=turn.scope,
                plan=plan(queue_allowance=float(limit)),
                within=core.Within(limit=float(limit)),
                call_id=call_id,
            ),
        )
        draining = result.state
        submissions = [row for row in result.requests if isinstance(row, core.SubmitMeasurement)]
        if not submissions:
            continue
        assert permit is not None
        request_id = submissions[0].request_id
        assert request_id is not None
        assert _deadline(draining, request_id) <= permit.deadline_at
        answered = next(call for call in draining.evaluation.agent_calls if call.call_id == call_id)
        assert answered.deadline_at is not None
        assert answered.deadline_at <= permit.deadline_at
        channel.prepare_evaluation(request_id.root, turn.scope, permit=permit)
        channel.start_execution(lambda: None, evaluation_id=request_id.root)
