"""Crash points of the skeleton run and the plans that crash at them (shared by the crash tests)."""

import asyncio
import tempfile
from functools import cache
from pathlib import Path

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
    """Whether this write is a dispatch authorization: the effect is the very next crossing."""
    following = crossings[index + 1 : index + 2]
    return (
        crossings[index].boundary == Boundary.DURABLE_WRITE
        and bool(following)
        and following[0].boundary == Boundary.EXECUTOR_REQUEST
    )


def crash_points() -> tuple[Crossing, ...]:
    """Crash points after an effect ran or after a write that is not a dispatch authorization."""
    crossings = all_crossings()
    return tuple(c for i, c in enumerate(crossings) if not before_an_effect(crossings, i))


def pre_effect_points() -> tuple[Crossing, ...]:
    """Dispatch authorizations: the host dies with the request durable and its effect not run."""
    crossings = all_crossings()
    return tuple(c for i, c in enumerate(crossings) if before_an_effect(crossings, i))


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
