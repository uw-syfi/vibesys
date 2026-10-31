"""A host that dies several times in one run, at generated points, is replaced by one that ends it.

`test_crash_convergence.py` kills the host once, or twice with the second death inside the first
recovery. Here a walk kills it two to five times: each death is drawn from the crossings (or
commits) of the host that the previous death started, and about half of the later deaths fall in
the first few crossings of that host, which is its recovery. A death inside a recovery that
itself follows a death inside a recovery is reachable only this way.

The skeleton walk keeps what no death may change: the run ends, and each job and each agent turn
ran once. A request's limit counts only time a host was alive (each restart moves it by the
downtime), but the run's own deadline does not move, so several deaths can keep the run down past
it and its outcome may differ from the straight run's; when it does not, the run adopted the same
tree.
The flow walks run real searches (dynamic, evolve, single, multi and the issue queue) through
`vs_dst`, whose crash cases start from a copy of the world instead of running the prefix again. For them a death may change the
result, so a walk holds what no death may change: the run ends, it owes nothing
(`liveness_violations`), no effect ran twice, and one more host started after its last commit
finds nothing to do.

A pull request walks one dynamic run through two deaths (one drawn, then one after its last
commit); the nightly job (`--depth=nightly`) walks many runs of each kind through up to five.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import Phase, given, settings
from hypothesis import strategies as st
from tests.support.crash_harness import (
    after_crash,
    all_crossings,
    ends_blocked,
    rule,
    run,
    straight_run,
)
from tests.system.crash._flow_worlds import WORLDS
from tests.vibesys.dynamic_world import DynamicWorld

from vs_core.api import RunStatus
from vs_dst.api import CrashKind, CrashPoint, simulate
from vs_faults.api import FaultPlan
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_dst.api import SimulatedRun
    from vs_faults.api import Crossing, FaultRule

_RECOVERY = 4
"""How many first crossings (or commits) of a restarted host a death "in recovery" falls among."""

# Shrinking re-runs a walk of several whole runs per step; a failure prints its walk instead.
_WALK = settings(deadline=None, database=None, phases=(Phase.explicit, Phase.generate))


def _death[T](draw: st.DataObject, offered: Sequence[T], *, restarted: bool) -> T:
    """One of ``offered``, among its first few when the draw puts the death in recovery."""
    if restarted and draw.draw(st.booleans(), label="in recovery"):
        offered = offered[:_RECOVERY]
    return draw.draw(st.sampled_from(offered))


@pytest.mark.slow
@settings(_WALK, max_examples=examples(pr=1, nightly=30))
@given(draw=st.data(), deaths=st.integers(min_value=2, max_value=5))
def test_a_skeleton_run_that_dies_several_times_ends_and_runs_each_effect_once(
    draw: st.DataObject, deaths: int
) -> None:
    _skeleton_walk(draw, deaths)


def _skeleton_walk(draw: st.DataObject, deaths: int) -> None:
    rules: list[FaultRule] = []
    calls: Sequence[Crossing] = all_crossings()
    offered = calls[:-1]
    blocked = False
    for _ in range(deaths):
        if not offered:
            break
        crossing = _death(draw, offered, restarted=bool(rules))
        blocked = blocked or ends_blocked(calls, crossing)
        rules.append(rule(crossing))
        calls = run(FaultPlan(seed=len(rules), rules=tuple(rules))).gate.calls
        offered = after_crash(calls, crossing)[:-1]
    plan = FaultPlan(seed=len(rules), rules=tuple(rules))
    replay = f"replay with {plan.model_dump_json()}"
    summary, straight = run(plan).summary, straight_run().summary
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.status == RunStatus.TERMINAL, replay
    assert summary.crashes == len(rules), replay
    # A death that ends the run blocked stops it early: each effect ran at most once.
    assert (summary.outcome == "blocked") == blocked, replay
    if blocked:
        assert set(summary.sbatch_calls) <= {1}, replay
        assert len(summary.sbatch_calls) <= len(straight.sbatch_calls), replay
        assert set(summary.agent_dispatches) <= set(straight.agent_dispatches), replay
        return
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay
    if summary.outcome == straight.outcome:
        assert summary.adopted_tree == straight.adopted_tree, replay


def _walk(
    draw: st.DataObject, normal: SimulatedRun, deaths: int
) -> tuple[SimulatedRun, list[CrashPoint]]:
    """The run after ``deaths`` host deaths, each in the run the previous one started."""
    walked = normal
    path: list[CrashPoint] = []
    for _ in range(deaths):
        commits = sorted({point.commit for point in walked.crash_points()})
        commit = _death(draw, range(commits[0], commits[-1] + 1), restarted=bool(path))
        path.append(CrashPoint(draw.draw(st.sampled_from(CrashKind)), commit))
        walked = walked.rerun_with_crash(path[-1])
        assert walked.finished, path
    return walked, path


def _ends_and_owes_nothing(walked: SimulatedRun, why: object) -> None:
    assert walked.finished, why
    assert walked.halted is None, why
    assert walked.liveness_violations == (), why
    assert walked.no_effect_ran_twice(), why


@settings(_WALK, max_examples=examples(pr=1, nightly=15))
@given(
    draw=st.data(),
    deaths=st.integers(min_value=examples(pr=1, nightly=2), max_value=examples(pr=1, nightly=4)),
)
@pytest.mark.parametrize(
    "world",
    [
        "dynamic",
        pytest.param("dynamic-parallel", marks=pytest.mark.slow),
        pytest.param("evolve", marks=pytest.mark.slow),
        pytest.param("single", marks=pytest.mark.slow),
        pytest.param("multi", marks=pytest.mark.slow),
        pytest.param("issue-queue", marks=pytest.mark.slow),
    ],
)
def test_a_flow_run_that_dies_several_times_ends_and_owes_nothing(
    world: str, draw: st.DataObject, deaths: int
) -> None:
    _flow_walk(world, draw, deaths)


def _flow_walk(world: str, draw: st.DataObject, deaths: int) -> None:
    normal = simulate(WORLDS[world]())
    walked, path = _walk(draw, normal, deaths)
    _ends_and_owes_nothing(walked, path)
    path.append(CrashPoint(CrashKind.AFTER_COMMIT, walked.commits - 1))
    again = walked.rerun_with_crash(path[-1])
    _ends_and_owes_nothing(again, path)
    assert again.result == walked.result


@pytest.mark.slow
def test_a_limit_does_not_pass_while_a_dead_hosts_lease_runs_out() -> None:
    """The walk that found it: two hosts die right after a commit, and each successor waits
    for the dead host's lease before it starts.

    No host is alive during those waits, so the restart credits them to every open limit.
    Crediting only the time after the lease ended timed out a request no host could wait on,
    and the run stalled closing on the block of it.
    """
    path = [CrashPoint(CrashKind.AFTER_COMMIT, 135), CrashPoint(CrashKind.AFTER_COMMIT, 146)]
    walked = simulate(WORLDS["multi"]())
    for point in path:
        walked = walked.rerun_with_crash(point)
    _ends_and_owes_nothing(walked, path)


@pytest.mark.slow
def test_a_record_the_close_failed_is_proven_released_by_a_later_inspection() -> None:
    """The walk that found it: the run closes around a blocked render, failing it with nothing
    proven released; the next host inspects it and the executor answers "never started".

    Core keeps the failure and records the release the answer proves, so the run ends.
    """
    path = [
        CrashPoint(CrashKind.BEFORE_COMMIT, 201),
        CrashPoint(CrashKind.AFTER_COMMIT, 227),
        CrashPoint(CrashKind.BEFORE_COMMIT, 256),
    ]
    walked = simulate(DynamicWorld(max_in_flight=2))
    for point in path:
        walked = walked.rerun_with_crash(point)
    _ends_and_owes_nothing(walked, path)
