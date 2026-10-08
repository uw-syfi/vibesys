"""Properties of the per-round paid-attempt budget."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vibesys.orchestration.attempts import CloseRound, Implement, next_step

_budgets = st.integers(min_value=1, max_value=20)


@given(max_attempts=_budgets)
def test_a_fresh_round_starts_at_attempt_one(max_attempts: int) -> None:
    assert next_step(last_paid=None, max_attempts=max_attempts) == Implement(1)


@given(max_attempts=_budgets, data=st.data())
def test_a_paid_attempt_is_never_owed_again_and_the_budget_closes_the_round(
    max_attempts: int, data: st.DataObject
) -> None:
    last_paid = data.draw(st.integers(min_value=1, max_value=max_attempts))

    step = next_step(last_paid=last_paid, max_attempts=max_attempts)

    if last_paid == max_attempts:
        assert step == CloseRound()
    else:
        assert step == Implement(last_paid + 1)


@given(max_attempts=_budgets)
def test_following_the_steps_runs_exactly_the_budgeted_attempts(max_attempts: int) -> None:
    # The live loop re-asks after each paid attempt; it must terminate having paid each
    # attempt number once, in order.
    paid: list[int] = []
    while isinstance(
        step := next_step(last_paid=paid[-1] if paid else None, max_attempts=max_attempts),
        Implement,
    ):
        paid.append(step.attempt)

    assert paid == list(range(1, max_attempts + 1))
