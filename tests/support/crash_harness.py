"""Crash points of the skeleton run and the plans that crash at them (shared by the crash tests).

Which points a session tries is `tests/system/crash/_catalog.py`'s business."""

from __future__ import annotations

import tempfile
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.skeleton_faults import COMMIT, RECEIPT_BEGUN, RECEIPT_PREFIX
from tests.support.skeleton_sim import Simulation, Summary, simulate

from vs_core.api import ExecuteRegisteredOperation, RunStatus
from vs_faults.api import Boundary, Crossing, FaultPlan, FaultRule, HostFault
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Sequence

_RUNS: dict[str, Simulation] = {}


def run(plan: FaultPlan) -> Simulation:
    """The finished run under ``plan``, simulated once per process (runs are deterministic).

    A double-crash test reuses the single-crash run of its first crash this way.
    """
    key = plan.model_dump_json()
    if key not in _RUNS:
        with tempfile.TemporaryDirectory() as tmp:
            _RUNS[key] = run_virtual(VirtualClock(), simulate(Path(tmp), plan))
    return _RUNS[key]


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


def after_crash(calls: Sequence[Crossing], crash: Crossing) -> tuple[Crossing, ...]:
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


_REGISTERED_OPERATION = ExecuteRegisteredOperation.model_fields["kind"].default


def ends_blocked(calls: Sequence[Crossing], crossing: Crossing) -> bool:
    """Whether a host death at ``crossing`` of the run that made ``calls`` ends the run blocked.

    That is a death right after a registered operation's begun marker: the operation began and
    never sealed, and no inspection can tell whether its effect ran. Recovery stays blocked, so
    the run loop ends the run with a typed ``blocked`` result (``recovery blocked: outcome
    unknown``) instead of guessing. This is the intended answer, not a gap to close: the point is
    named by what it is, so a change in the order of writes cannot exclude a different one.
    """
    if crossing.target != RECEIPT_BEGUN:
        return False
    before = calls[: calls.index(crossing)]
    request = next(c for c in reversed(before) if c.boundary == Boundary.EXECUTOR_REQUEST)
    return request.target == _REGISTERED_OPERATION


def ends_as_it_must(crossing: Crossing) -> None:
    """Crash once at ``crossing``: the run converges, or ends blocked where recovery cannot tell."""
    if not ends_blocked(all_crossings(), crossing):
        converges_after_one(crossing)
        return
    plan = crash_plan(crossing)
    summary = run(plan).summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.status == RunStatus.TERMINAL, replay
    assert summary.outcome == "blocked", replay


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


def assert_converges(summary: Summary, replay: str, *, crashes: int) -> None:
    """The run ended as the straight run does after ``crashes`` host deaths."""
    straight = straight_run().summary
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.crashes == crashes, replay
    assert summary.outcome == straight.outcome, replay
    assert summary.adopted_tree == straight.adopted_tree, replay
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay


def converges_after_one(crossing: Crossing) -> None:
    """Crash once at ``crossing``: the run ends as the straight run."""
    plan = crash_plan(crossing)
    assert_converges(run(plan).summary, f"replay with {plan.model_dump_json()}", crashes=1)


def converges_after(first: Crossing, second: Crossing) -> None:
    """Crash at ``first``, then at ``second`` in the recovery run: the run ends as the straight run."""
    plan = FaultPlan(seed=second.ordinal, rules=(rule(first), rule(second)))
    assert_converges(run(plan).summary, f"replay with {plan.model_dump_json()}", crashes=2)
