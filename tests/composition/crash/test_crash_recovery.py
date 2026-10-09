"""A host crash after any boundary of the skeleton run converges to the crash-free result."""

from __future__ import annotations

import pytest
from tests.support.crash_harness import (
    crash_plan,
    crash_points,
    name,
    run,
    straight_run,
)

from vs_core.api import RunStatus
from vs_faults.api import Boundary, Crossing


def test_the_fault_free_run_succeeds_once_per_effect() -> None:
    summary = straight_run().summary
    assert summary.stalled is None
    assert summary.status == RunStatus.TERMINAL
    assert summary.outcome == "success"
    assert summary.adopted_tree is not None
    assert summary.crashes == 0
    assert summary.sbatch_calls
    assert summary.sbatch_calls == (1,) * len(summary.sbatch_calls)
    assert summary.agent_dispatches
    assert [count for _, count in summary.agent_dispatches] == [1] * len(summary.agent_dispatches)


def test_every_boundary_has_crash_points() -> None:
    boundaries = {crossing.boundary for crossing in crash_points()}
    assert boundaries == {Boundary.EXECUTOR_REQUEST, Boundary.DURABLE_WRITE}


@pytest.mark.parametrize(
    "crossing",
    [c for c in crash_points() if c.boundary == Boundary.EXECUTOR_REQUEST],
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
