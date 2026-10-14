"""A host that dies at any boundary of the skeleton run is replaced by one that ends the run alike.

The skeleton is the one search here: what the shell guarantees (intent before effect, recovery
in a new epoch, an effect that ran is never repeated) does not depend on the search, so a search
that is correct on core's contract inherits it. The crash points are grouped by what the host had
made durable (`PointKind`) and by the lifecycle class of the request in flight; a pull request
crashes the host once per group, the nightly job (`--depth=nightly`) at every point of each.
Whatever the crash, the run ends exactly as the straight run does: the same result, the same adopted
tree, one cluster submission per measurement and one agent dispatch per turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import (
    assert_converges,
    converges_after,
    crash_plan,
    ends_as_it_must,
    run,
    straight_run,
)
from tests.system.crash._catalog import (
    Point,
    PointKind,
    groups,
    params,
    recovery_parts,
    run_with_lost_seal,
    sampled,
    sampled_recoveries,
)

from vs_core.api import LifecycleClass, RunStatus

if TYPE_CHECKING:
    from tests.support.crash_harness import Simulation

    from vs_faults.api import Crossing


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


def test_every_kind_of_point_reaches_every_lifecycle_class_of_request() -> None:
    """A pull request's sample misses no class of request, whichever way the host dies."""
    for kind in set(PointKind) - {PointKind.RECOVERY, PointKind.TRANSITION}:
        assert set(groups(kind)) == set(LifecycleClass), kind
    assert groups(PointKind.TRANSITION)


def _check(crossing: Crossing) -> None:
    ends_as_it_must(crossing)
    assert run(crash_plan(crossing)).summary.status == RunStatus.TERMINAL


ONE_CRASH = (
    PointKind.OBSERVATION_LOST,
    PointKind.AUTHORIZED,
    PointKind.BEGUN,
    PointKind.SEALED,
    PointKind.TRANSITION,
)


@pytest.mark.parametrize("point", params([p for kind in ONE_CRASH for p in sampled(kind)]))
def test_a_crash_at_a_boundary_ends_the_run_as_the_straight_run_does(point: Point) -> None:
    _check(point.crossing)


def _lost_seals() -> list[object]:
    return params(sampled(PointKind.SEAL_LOST))


@pytest.mark.parametrize("point", _lost_seals())
def test_a_crash_before_a_result_is_sealed_ends_the_run_as_the_straight_run_does(
    point: Point,
) -> None:
    seal: Simulation = run_with_lost_seal(point.crossing)
    assert_converges(seal.summary, f"lost seal at {point.crossing}", crashes=1)


@pytest.mark.parametrize("part", range(recovery_parts()))
@pytest.mark.parametrize("lifecycle", list(LifecycleClass))
def test_a_crash_during_recovery_ends_the_run_as_the_straight_run_does(
    lifecycle: LifecycleClass, part: int
) -> None:
    for point in sampled_recoveries(lifecycle, part, recovery_parts()):
        assert point.first is not None
        converges_after(point.first, point.crossing)
