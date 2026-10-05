"""Crash points of the skeleton run and the plans that crash at them (shared by the crash tests)."""

import asyncio
import tempfile
from functools import cache
from pathlib import Path

from tests.support.skeleton_faults import COMMIT, RECEIPT_BEGUN, RECEIPT_PREFIX
from tests.support.skeleton_sim import Simulation, simulate

from vs_faults.api import Boundary, Crossing, FaultPlan, FaultRule, HostFault


def run(plan: FaultPlan) -> Simulation:
    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(simulate(Path(tmp), plan))


@cache
def straight_run() -> Simulation:
    return run(FaultPlan(seed=0))


def all_crossings() -> tuple[Crossing, ...]:
    """Every executor request and durable write of the fault-free run, in order."""
    return tuple(straight_run().gate.calls)


def before_an_effect(crossings: tuple[Crossing, ...], index: int) -> bool:
    """Whether the host dies with the request authorized or begun and its effect not run.

    That is a dispatch authorization (the effect is the very next crossing) or the begun
    marker ``run_once`` writes just before the effect.
    """
    if crossings[index].target == RECEIPT_BEGUN:
        return True
    following = crossings[index + 1 : index + 2]
    return (
        crossings[index].boundary == Boundary.DURABLE_WRITE
        and crossings[index].target == COMMIT
        and bool(following)
        and following[0].boundary == Boundary.EXECUTOR_REQUEST
    )


def crash_points() -> tuple[Crossing, ...]:
    """Crash points after an effect ran or after a write that is not a dispatch authorization."""
    crossings = all_crossings()
    return tuple(c for i, c in enumerate(crossings) if not before_an_effect(crossings, i))


def pre_effect_points() -> tuple[Crossing, ...]:
    """Dispatch authorizations and begun markers: the request is durable, its effect has not run."""
    crossings = all_crossings()
    return tuple(c for i, c in enumerate(crossings) if before_an_effect(crossings, i))


def after_crash(calls: list[Crossing], crash: Crossing) -> tuple[Crossing, ...]:
    """The crossings of the restarted host: those recorded after the crash point.

    An executor request is recorded when it is entered, so the receipt writes it makes (begun
    marker, sealed result) are listed right after it but happened before its crash fired.
    """
    rest = calls[calls.index(crash) + 1 :]
    skipped = 0
    if crash.boundary == Boundary.EXECUTOR_REQUEST:
        while skipped < len(rest) and rest[skipped].target.startswith(RECEIPT_PREFIX):
            skipped += 1
    return tuple(rest[skipped:])


def name(crossing: Crossing) -> str:
    return f"{crossing.boundary.value}:{crossing.target}#{crossing.ordinal}"


def crash_plan(crossing: Crossing) -> FaultPlan:
    rule = FaultRule(
        boundary=crossing.boundary,
        target=crossing.target,
        at=crossing.ordinal,
        fault=HostFault.CRASH_AFTER,
    )
    return FaultPlan(seed=crossing.ordinal, rules=(rule,))


def rule(crossing: Crossing) -> FaultRule:
    return FaultRule(
        boundary=crossing.boundary,
        target=crossing.target,
        at=crossing.ordinal,
        fault=HostFault.CRASH_AFTER,
    )


def following(first: Crossing, depth: int) -> tuple[Crossing, ...]:
    """The next ``depth`` crossings of the run that restarts after a crash at ``first``."""
    return after_crash(run(crash_plan(first)).gate.calls, first)[:depth]


def converges_after(first: Crossing, second: Crossing) -> None:
    """Crash at ``first``, then at ``second`` in the recovery run: the run ends as the straight run."""
    plan = FaultPlan(seed=second.ordinal, rules=(rule(first), rule(second)))
    summary = run(plan).summary
    straight = straight_run().summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.crashes == 2, replay
    assert summary.outcome == straight.outcome, replay
    assert summary.adopted_tree == straight.adopted_tree, replay
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay
