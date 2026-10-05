"""Publication joins observations in the lifecycle order, never losing news."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import EvaluationState, ExecutorObservation
from vs_sandbox.api import join_observation

_TERMINAL = {EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED}
_ORDER = [
    EvaluationState.QUEUED,
    EvaluationState.STARTING,
    EvaluationState.RUNNING,
    EvaluationState.CANCELING,
]


@st.composite
def _observations(draw: st.DrawFn) -> ExecutorObservation:
    state = draw(st.sampled_from([*_ORDER, *sorted(_TERMINAL, key=lambda s: s.value)]))
    staged = state not in {EvaluationState.SUCCEEDED, EvaluationState.FAILED}
    stage = draw(st.sampled_from([None, "accuracy", "benchmark"])) if staged else None
    failure = "boom" if state is EvaluationState.FAILED else None
    return ExecutorObservation(state=state, current_stage=stage, failure=failure)


def _rank(state: EvaluationState) -> int:
    return _ORDER.index(state) if state in _ORDER else len(_ORDER)


@given(held=_observations(), new=_observations())
def test_the_join_holds_the_highest_state_and_re_joining_changes_nothing(
    held: ExecutorObservation, new: ExecutorObservation
) -> None:
    joined = join_observation(held, new)
    assert _rank(joined.state) == max(_rank(held.state), _rank(new.state))
    assert join_observation(joined, new) == joined


@given(held=_observations(), new=_observations())
def test_the_first_terminal_state_wins(held: ExecutorObservation, new: ExecutorObservation) -> None:
    if held.state in _TERMINAL:
        assert join_observation(held, new) == held


@given(held=_observations(), new=_observations())
def test_a_tie_keeps_the_stage_the_newer_reading_omits(
    held: ExecutorObservation, new: ExecutorObservation
) -> None:
    joined = join_observation(held, new)
    if held.state is new.state and held.state not in _TERMINAL and new.current_stage is None:
        assert joined.current_stage == held.current_stage
