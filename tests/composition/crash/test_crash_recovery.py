"""A host crash after any boundary of the skeleton run converges to the crash-free result."""

from __future__ import annotations

import pytest
from tests.support.crash_harness import (
    converges_after_one,
    crash_points,
    name,
    representatives,
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


def _requests() -> list[Crossing]:
    return [c for c in crash_points() if c.boundary == Boundary.EXECUTOR_REQUEST]


@pytest.mark.parametrize("crossing", representatives().requests, ids=name)
def test_a_crash_after_the_first_request_of_each_kind_converges(crossing: Crossing) -> None:
    converges_after_one(crossing)


@pytest.mark.slow
@pytest.mark.parametrize("crossing", _requests(), ids=name)
def test_a_crash_after_each_boundary_converges(crossing: Crossing) -> None:
    converges_after_one(crossing)
