"""The strategy answers every refusal by whether it can be retried, with no stuck phase.

A refusal core gives for now (the run is held) leaves no receipt, so the strategy proposes
the same decision again; a refusal for good ends what depended on it. Each subject that
proposes (winner, planner, baseline, workstream) follows that rule for every rejection code.
"""

from __future__ import annotations

import pytest
from tests.vibesys.orchestration.dynamic.strategy._run import config
from tests.vibesys.orchestration.dynamic.strategy._views import empty_view

from vibesys.orchestration.dynamic.strategy.api import (
    BaselineStage,
    BaselineState,
    DynamicStrategy,
    DynamicStrategyState,
    PlannerState,
    RunPhase,
    Step,
    decision_id,
)
from vs_core.api import DecisionId, Rejected, RejectionCode, RejectionOutlook, rejection_outlook

_CODES = list(RejectionCode)


def _rejection(decision: DecisionId, code: RejectionCode) -> Rejected:
    return Rejected(decision_id=decision, code=code, path=("scope",), detail="refused")


def _fold(state: DynamicStrategyState, event: Rejected) -> DynamicStrategyState:
    strategy = DynamicStrategy(config=config()).bind(state)
    return strategy.on_event(empty_view(), event)


@pytest.mark.parametrize("code", _CODES)
def test_a_refused_winner_proposal_never_leaves_the_strategy_adopting(code: RejectionCode) -> None:
    adopting = DynamicStrategyState(phase=RunPhase.ADOPTING)

    after = _fold(adopting, _rejection(decision_id("propose", "run"), code))

    assert after.phase is not RunPhase.ADOPTING
    expected = (
        RunPhase.SELECTING
        if rejection_outlook(code) is RejectionOutlook.NOT_NOW
        else RunPhase.STOPPING
    )
    assert after.phase is expected
    assert after.winner is None


def test_only_a_stale_or_held_run_is_a_refusal_for_now() -> None:
    assert {code for code in _CODES if rejection_outlook(code) is RejectionOutlook.NOT_NOW} == {
        RejectionCode.STALE_VIEW,
        RejectionCode.HELD,
    }


def test_a_baseline_measurement_refused_for_now_is_asked_again_and_for_good_is_unmeasurable() -> (
    None
):
    awaiting = decision_id("measure", "baseline", 0)
    waiting = DynamicStrategyState(
        baseline=BaselineState(stage=BaselineStage.AWAITING, awaiting=awaiting, attempts=1)
    )

    now = _fold(waiting, _rejection(awaiting, RejectionCode.HELD)).baseline
    never = _fold(waiting, _rejection(awaiting, RejectionCode.CLOSED_SCOPE)).baseline

    assert (now.stage, now.awaiting, now.attempts) == (BaselineStage.NEEDED, None, 0)
    assert never.stage is BaselineStage.UNMEASURABLE
    assert never.awaiting is None


def test_a_planner_decision_refused_for_now_is_asked_again_and_for_good_ends_the_call() -> None:
    awaiting = decision_id("render", "planner", 0)
    waiting = DynamicStrategyState(
        planner=PlannerState(step=Step.RENDERING, awaiting=awaiting, active=True)
    )

    now = _fold(waiting, _rejection(awaiting, RejectionCode.HELD)).planner
    never = _fold(waiting, _rejection(awaiting, RejectionCode.CLOSED_SCOPE)).planner

    assert (now.step, now.awaiting) == (Step.NEEDED, None)
    assert never.awaiting is None
    assert never.last_error is not None
