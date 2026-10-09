"""Properties of the per-round paid-attempt budget."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from vibesys.orchestration.attempts import AttemptKey, CloseRound, Implement, next_step

_budgets = st.integers(min_value=1, max_value=20)
_KEY = AttemptKey(round_number=3, member_id="H-01")


@dataclass(frozen=True)
class _Marker:
    round_number: int
    member_id: str
    turn_number: int


@given(max_attempts=_budgets)
def test_no_marker_starts_at_attempt_one(max_attempts: int) -> None:
    assert next_step(marker=None, key=_KEY, max_attempts=max_attempts) == Implement(1)


@given(
    max_attempts=_budgets,
    other_round=st.integers(min_value=1, max_value=10).filter(lambda n: n != _KEY.round_number),
    turn=st.integers(min_value=1, max_value=20),
)
def test_a_marker_for_another_round_or_member_does_not_spend_this_budget(
    max_attempts: int, other_round: int, turn: int
) -> None:
    for marker in (
        _Marker(other_round, _KEY.member_id, turn),
        _Marker(_KEY.round_number, "H-02", turn),
    ):
        assert next_step(marker=marker, key=_KEY, max_attempts=max_attempts) == Implement(1)


@given(max_attempts=_budgets, data=st.data())
def test_a_paid_attempt_is_never_owed_again_and_the_budget_closes_the_round(
    max_attempts: int, data: st.DataObject
) -> None:
    turn = data.draw(st.integers(min_value=1, max_value=max_attempts))
    marker = _Marker(_KEY.round_number, _KEY.member_id, turn)

    step = next_step(marker=marker, key=_KEY, max_attempts=max_attempts)

    assert step == (CloseRound() if turn == max_attempts else Implement(turn + 1))
