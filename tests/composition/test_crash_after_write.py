"""A host crash after any durable write of the skeleton run converges to the crash-free result."""

from __future__ import annotations

import pytest
from tests.support.crash_harness import (
    crash_plan,
    crash_points,
    name,
    run,
    straight_run,
)

from vs_faults.api import Boundary, Crossing


@pytest.mark.parametrize(
    "crossing",
    [c for c in crash_points() if c.boundary == Boundary.DURABLE_WRITE],
    ids=name,
)
def test_a_crash_after_each_boundary_converges(crossing: Crossing) -> None:
    plan = crash_plan(crossing)
    summary = run(plan).summary
    straight = straight_run().summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.crashes == 1, replay
    assert summary.outcome == straight.outcome, replay
    assert summary.adopted_tree == straight.adopted_tree, replay
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay
