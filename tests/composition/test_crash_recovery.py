"""A host crash at any boundary of the skeleton run converges to the crash-free result.

The crash points come from the run itself: a fault-free run records every executor request it
dispatched and every durable write it made (``FaultGate.calls``). For each of them the test
runs the same scenario with a plan that crashes the host right after that call, restarts a
fresh host over the same Project, and requires the same terminal result, the same adopted tree
and the same external effects by idempotency key (one sbatch and one agent dispatch per key).
A crash after a durable write covers both commit orders: the observation committed with its
owner events still pending, and after each owner event applies. A new request kind adds
crossings to the fault-free run and so adds crash points without new test code.
"""

from __future__ import annotations

import asyncio
import tempfile
from functools import cache
from pathlib import Path

import pytest
from tests.support.skeleton_sim import Simulation, simulate

from vs_core.api import RunStatus
from vs_faults.api import Boundary, Crossing, FaultPlan, FaultRule, HostFault


def _run(plan: FaultPlan) -> Simulation:
    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(simulate(Path(tmp), plan))


@cache
def _straight() -> Simulation:
    return _run(FaultPlan(seed=0))


def _crossings() -> tuple[Crossing, ...]:
    """Every executor request and durable write of the fault-free run, in order."""
    return tuple(_straight().gate.calls)


def _before_an_effect(crossings: tuple[Crossing, ...], index: int) -> bool:
    """Whether this write is a dispatch authorization: the effect is the very next crossing."""
    following = crossings[index + 1 : index + 2]
    return (
        crossings[index].boundary == Boundary.DURABLE_WRITE
        and bool(following)
        and following[0].boundary == Boundary.EXECUTOR_REQUEST
    )


def crash_points() -> tuple[Crossing, ...]:
    """Crash points after an effect ran or after a write that is not a dispatch authorization."""
    crossings = _crossings()
    return tuple(c for i, c in enumerate(crossings) if not _before_an_effect(crossings, i))


def pre_effect_points() -> tuple[Crossing, ...]:
    """Dispatch authorizations: the host dies with the request durable and its effect not run."""
    crossings = _crossings()
    return tuple(c for i, c in enumerate(crossings) if _before_an_effect(crossings, i))


def _name(crossing: Crossing) -> str:
    return f"{crossing.boundary.value}:{crossing.target}#{crossing.ordinal}"


def _plan(crossing: Crossing) -> FaultPlan:
    rule = FaultRule(
        boundary=crossing.boundary,
        target=crossing.target,
        at=crossing.ordinal,
        fault=HostFault.CRASH_AFTER,
    )
    return FaultPlan(seed=crossing.ordinal, rules=(rule,))


def test_the_fault_free_run_succeeds_once_per_effect() -> None:
    summary = _straight().summary
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


@pytest.mark.parametrize("crossing", crash_points(), ids=_name)
def test_a_crash_after_each_boundary_converges(crossing: Crossing) -> None:
    plan = _plan(crossing)
    summary = _run(plan).summary
    straight = _straight().summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.crashes == 1, replay
    assert summary.outcome == straight.outcome, replay
    assert summary.adopted_tree == straight.adopted_tree, replay
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay


# A host that dies after authorizing a CloseSession and before running it leaves the session
# CLOSING: the restart inspects the request, finds it never began and reports it REJECTED, and
# nothing issues it again. The no-orphan-waits check names the stuck session at that commit.
_NEVER_STARTED_CLOSE = frozenset({"durable_write:commit#52"})


def _pre_effect_points() -> list[object]:
    return [
        pytest.param(
            crossing,
            id=_name(crossing),
            marks=[
                pytest.mark.xfail(
                    strict=True,
                    reason="a never-started CloseSession is rejected and the session stays CLOSING",
                )
            ]
            if _name(crossing) in _NEVER_STARTED_CLOSE
            else [],
        )
        for crossing in pre_effect_points()
    ]


@pytest.mark.parametrize("crossing", _pre_effect_points())
def test_a_crash_before_an_effect_never_repeats_one(crossing: Crossing) -> None:
    """The lost effect is reported to the strategy as a rejection (it never began), so the run
    may legitimately end differently: only the effects that did run must not repeat.
    """
    plan = _plan(crossing)
    summary = _run(plan).summary
    straight = _straight().summary
    replay = f"replay with {plan.model_dump_json()}"
    assert max(summary.sbatch_calls, default=0) <= 1, replay
    assert max((n for _, n in summary.agent_dispatches), default=0) <= 1, replay
    assert len(summary.sbatch_calls) <= len(straight.sbatch_calls), replay


# A second crash while the restarted host is still recovering. The first crash is sampled
# (the first call of each request kind: the effect ran and its observation was lost). The
# second is every crossing of the recovery window: from the restart until the first request
# that is not an inspection, so each recovery write and each inspection is a crash point.
_INSPECTION = "inspect_request"


def _first_of_each_kind() -> tuple[Crossing, ...]:
    seen: set[str] = set()
    first = []
    for crossing in crash_points():
        if crossing.boundary == Boundary.EXECUTOR_REQUEST and crossing.target not in seen:
            seen.add(crossing.target)
            first.append(crossing)
    return tuple(first)


@cache
def _recovery_window(first: Crossing) -> tuple[Crossing, ...]:
    calls = _run(_plan(first)).gate.calls
    window: list[Crossing] = []
    for crossing in calls[calls.index(first) + 1 :]:
        if crossing.boundary == Boundary.EXECUTOR_REQUEST and crossing.target != _INSPECTION:
            break
        window.append(crossing)
    # The write that authorizes the first ordinary request is a dispatch, not recovery.
    return tuple(window[:-1]) if window and window[-1].boundary == Boundary.DURABLE_WRITE else ()


# Known gap: a restart that crashes again while recovering re-issues a measurement poll under
# the identity of one already prepared, with a later deadline, and core rejects the conflict.
_REISSUED_POLL = frozenset({"executor_request:submit_measurement#1+durable_write:commit#11"})


def _sampled(window: tuple[Crossing, ...]) -> tuple[Crossing, ...]:
    """The harness budget is three minutes: every third crossing of a window, and its last."""
    return tuple(c for i, c in enumerate(window) if i % 3 == 0 or i == len(window) - 1)


def _double_crashes() -> list[object]:
    marks = [
        pytest.mark.xfail(
            strict=True, reason="a poll is re-issued under its prepared identity after a re-crash"
        )
    ]
    return [
        pytest.param(
            first,
            second,
            id=f"{_name(first)}+{_name(second)}",
            marks=marks if f"{_name(first)}+{_name(second)}" in _REISSUED_POLL else [],
        )
        for first in _first_of_each_kind()
        for second in _sampled(_recovery_window(first))
    ]


def _rule(crossing: Crossing) -> FaultRule:
    return FaultRule(
        boundary=crossing.boundary,
        target=crossing.target,
        at=crossing.ordinal,
        fault=HostFault.CRASH_AFTER,
    )


@pytest.mark.parametrize(("first", "second"), _double_crashes())
def test_a_crash_during_recovery_still_converges(first: Crossing, second: Crossing) -> None:
    plan = FaultPlan(seed=second.ordinal, rules=(_rule(first), _rule(second)))
    summary = _run(plan).summary
    straight = _straight().summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.crashes == 2, replay
    assert summary.outcome == straight.outcome, replay
    assert summary.adopted_tree == straight.adopted_tree, replay
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay
