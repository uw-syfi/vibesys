"""Publication joins observations in the lifecycle order, never losing news."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationState,
    ExecutorObservation,
    LifecyclePublisher,
    is_finished,
    join_observation,
    state_rank,
)
from vs_evaluation.api.testing import FakeEvaluationExecutor
from vs_sim.api.testing import ManualClock

_ORDER = [
    EvaluationState.QUEUED,
    EvaluationState.STARTING,
    EvaluationState.RUNNING,
    EvaluationState.CANCELING,
]
_HANDLE = "handle"


@st.composite
def _observations(draw: st.DrawFn) -> ExecutorObservation:
    state = draw(st.sampled_from(list(EvaluationState)))
    staged = state not in {EvaluationState.SUCCEEDED, EvaluationState.FAILED}
    stage = draw(st.sampled_from([None, "accuracy", "benchmark"])) if staged else None
    failure = "boom" if state is EvaluationState.FAILED else None
    return ExecutorObservation(state=state, current_stage=stage, failure=failure)


def test_every_state_is_either_finished_or_in_the_visiting_order() -> None:
    assert {state for state in EvaluationState if not is_finished(state)} == set(_ORDER)
    assert [state_rank(state) for state in _ORDER] == sorted(state_rank(state) for state in _ORDER)
    assert {state_rank(state) for state in EvaluationState if is_finished(state)} == {len(_ORDER)}


@given(held=_observations(), new=_observations())
def test_the_join_holds_the_highest_state_and_re_joining_changes_nothing(
    held: ExecutorObservation, new: ExecutorObservation
) -> None:
    joined = join_observation(held, new)
    assert state_rank(joined.state) == max(state_rank(held.state), state_rank(new.state))
    assert join_observation(joined, new) == joined


@given(held=_observations(), new=_observations())
def test_the_first_finished_state_wins(held: ExecutorObservation, new: ExecutorObservation) -> None:
    if is_finished(held.state):
        assert join_observation(held, new) == held


@given(held=_observations(), new=_observations())
def test_a_tie_keeps_the_stage_the_newer_reading_omits(
    held: ExecutorObservation, new: ExecutorObservation
) -> None:
    joined = join_observation(held, new)
    if held.state is new.state and not is_finished(held.state) and new.current_stage is None:
        assert joined.current_stage == held.current_stage


@given(published=st.lists(_observations(), min_size=1, max_size=12))
def test_a_publisher_never_moves_a_handle_backwards_or_out_of_a_finished_state(
    published: list[ExecutorObservation],
) -> None:
    publisher = LifecyclePublisher()
    held_ranks: list[int] = []
    first_finished: ExecutorObservation | None = None
    for observation in published:
        held = publisher.publish(_HANDLE, observation)
        assert publisher.observation(_HANDLE) == held
        held_ranks.append(state_rank(held.state))
        if first_finished is None and is_finished(held.state):
            first_finished = held
        if first_finished is not None:
            assert held == first_finished
    assert held_ranks == sorted(held_ranks)


@pytest.mark.asyncio
async def test_a_fake_executor_cancel_keeps_the_result_of_a_finished_evaluation() -> None:
    executor = FakeEvaluationExecutor(clock=ManualClock())
    executor.backend.publish(_HANDLE, ExecutorObservation(state=EvaluationState.SUCCEEDED))

    await executor.cancel(_HANDLE)

    observed = await executor.inspect(_HANDLE)
    assert observed is not None
    assert observed.state is EvaluationState.SUCCEEDED
