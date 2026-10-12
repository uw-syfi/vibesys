"""A planner that parks or abandons a finished hypothesis leaves a state the run can keep.

Regression for #1594: the strategy stored the update's disposition as the raw string
in the hypothesis record, and the run died revalidating its state on the next commit.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.strategy._harness import envelope, round_trip
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
)
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vibesys.hypothesis import HypothesisStrategy
from vibesys.orchestration.dynamic.models import StrategyReason
from vibesys.orchestration.dynamic.strategy.api import DynamicStrategyState
from vs_core.api import Stop
from vs_dst.api import simulate


def _second_plan(disposition: str, reason_kind: str = "blocked") -> str:
    return json.dumps(
        {
            "reasoning": "retire h1, try h2",
            "workstreams": [implement("h2")],
            "hypothesis_updates": [
                {
                    "hypothesis_id": "h1",
                    "disposition": disposition,
                    "reason_kind": reason_kind,
                    "reason": "no further progress",
                }
            ],
        }
    )


def _blocked() -> str:
    return implemented("implementation_failed", summary="blocked", next_step="same step")


@pytest.mark.parametrize("disposition", ["parked", "abandoned"])
def test_a_run_survives_the_planner_retiring_a_finished_hypothesis(disposition: str) -> None:
    world = DynamicWorld(
        planner=[plan_reply(implement("h1")), _second_plan(disposition)],
        implementer=[_blocked(), _blocked(), implemented()],
        max_rounds=2,
        max_retries_per_round=5,
    )

    run = simulate(world)

    assert run.finished
    assert not run.liveness_violations
    assert world.unused["orchestrator"] == 0, "the retiring plan was never asked for"
    final = run.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    disposition=st.sampled_from(["parked", "abandoned"]),
    reason_kind=st.sampled_from([item.value for item in StrategyReason]),
)
def test_every_retirement_the_planner_schema_allows_leaves_a_persistable_state(
    disposition: str, reason_kind: str
) -> None:
    world = DynamicWorld(
        planner=[plan_reply(implement("h1")), _second_plan(disposition, reason_kind)],
        implementer=[_blocked(), _blocked(), implemented()],
        max_rounds=2,
        max_retries_per_round=5,
    )

    run = simulate(world)
    state = run.state

    assert run.finished
    assert not run.liveness_violations
    assert isinstance(state, DynamicStrategyState)

    retired = next(item for item in state.hypotheses if item.hypothesis_id == "h1")
    assert retired.strategy is HypothesisStrategy(disposition)
    assert retired.reason_kind == reason_kind
    assert DynamicStrategyState.model_validate(state.model_dump(mode="python")) == state
    codec, saved = envelope(state)
    assert round_trip(codec, saved).strategy == state
