"""Crash points of the skeleton run and the plans that crash at them (shared by the crash tests)."""

import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests.support.skeleton_faults import COMMIT, RECEIPT_BEGUN, RECEIPT_PREFIX, RECEIPT_SEALED
from tests.support.skeleton_sim import Simulation, Summary, simulate

from vs_faults.api import Boundary, Crossing, FaultPlan, FaultRule, HostFault
from vs_sim.api.testing import VirtualClock, run_virtual

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


@dataclass(frozen=True)
class Representatives:
    """One crash point per recovery path of the straight run (the fast tier of the sweeps).

    Recovery depends on which request kind was in flight and on how far it got, not on which
    of the 12 repeated lifecycle steps it was: so the first call of every request kind is
    crashed at its request (effect ran, observation lost), its ``begun`` marker (effect not
    run) and its sealed write (effect ran, seal lost). The commits between requests all advance
    one core state machine whatever the kind, so those are sampled: the authorization, the
    middle of the gap and the commit two after the seal for every third kind, plus the run's
    first commit and its last commits. Together they reach every recovery branch that crashing
    at all 163 crossings reaches (measured by branch arcs of src and libs). The exhaustive sweeps of every crossing are marked ``slow``.
    """

    requests: tuple[Crossing, ...]
    begun: tuple[Crossing, ...]
    sealed: tuple[Crossing, ...]
    commits: tuple[Crossing, ...]


@cache
def representatives() -> Representatives:
    """Pick the representatives from the straight run (see :class:`Representatives`)."""
    crossings = all_crossings()
    firsts: dict[str, int] = {}
    for index, crossing in enumerate(crossings):
        if crossing.boundary == Boundary.EXECUTOR_REQUEST:
            firsts.setdefault(crossing.target, index)
    at = list(firsts.values())
    commits: list[Crossing] = [crossings[0]]
    for index in at[::3]:
        previous = max(
            (i for i in range(index) if crossings[i].target == RECEIPT_SEALED), default=-1
        )
        commits += [
            crossings[index - 1],
            crossings[(previous + index) // 2 + 1],
            crossings[index + 4],
        ]
    last = len(crossings) - 1
    commits += [crossings[last - 3], crossings[last]]
    return Representatives(
        requests=tuple(crossings[i] for i in at),
        begun=tuple(crossings[i + 1] for i in at),
        sealed=tuple(crossings[i + 2] for i in at),
        commits=tuple(dict.fromkeys(commits)),
    )


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
