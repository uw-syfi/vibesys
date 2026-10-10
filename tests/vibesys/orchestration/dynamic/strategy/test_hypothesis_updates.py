"""A planner that parks or abandons a finished hypothesis leaves a state the run can keep.

Regression for #1594: the strategy stored the update's disposition as the raw string
in the hypothesis record, and the run died revalidating its state on the next commit.
"""

from __future__ import annotations

import json
from collections import deque

import pytest
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell

from vs_core.api import Stop


def _second_plan(disposition: str) -> str:
    return json.dumps(
        {
            "reasoning": "retire h1, try h2",
            "workstreams": [implement("h2")],
            "hypothesis_updates": [
                {
                    "hypothesis_id": "h1",
                    "disposition": disposition,
                    "reason_kind": "blocked",
                    "reason": "no further progress",
                }
            ],
        }
    )


def _blocked() -> str:
    return implemented("implementation_failed", summary="blocked", next_step="same step")


@pytest.mark.parametrize("disposition", ["parked", "abandoned"])
def test_a_run_survives_the_planner_retiring_a_finished_hypothesis(disposition: str) -> None:
    executors = Executors(
        planner=deque([plan_reply(implement("h1")), _second_plan(disposition)]),
        implementer=deque([_blocked(), _blocked(), implemented()]),
        judge=deque([reviewed()]),
    )

    trace = run_shell(executors, max_rounds=2, max_retries_per_round=5)

    assert not executors.planner, "the retiring plan was never asked for"
    final = trace.decisions[-1]
    assert isinstance(final, Stop)
    assert final.result.outcome == "success"
