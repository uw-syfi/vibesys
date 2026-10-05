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


# Crash points that fail today, by the finding that explains them (REVIEW-P4). Each is a strict
# expected failure: when its fix lands the marker turns into a failure and is removed.
_P1_1 = frozenset(
    {
        "executor_request:dispatch_turn#1",
        "executor_request:observe_owned_job#1",
        "executor_request:observe_owned_job#2",
    }
)

_P1_2 = frozenset(
    {
        "durable_write:commit#29",
        "durable_write:commit#30",
        "durable_write:commit#31",
        "durable_write:commit#33",
        "durable_write:commit#34",
        "durable_write:commit#35",
        "durable_write:commit#36",
        "durable_write:commit#38",
        "durable_write:commit#39",
        "durable_write:commit#40",
        "durable_write:commit#41",
        "durable_write:commit#42",
        "durable_write:commit#43",
        "durable_write:commit#45",
        "durable_write:commit#46",
        "durable_write:commit#47",
        "durable_write:commit#48",
        "durable_write:commit#49",
        "durable_write:commit#51",
        "durable_write:commit#53",
        "durable_write:commit#54",
        "durable_write:commit#56",
        "durable_write:commit#58",
        "durable_write:commit#59",
        "durable_write:commit#60",
        "durable_write:commit#61",
        "durable_write:commit#62",
        "durable_write:commit#64",
        "durable_write:commit#65",
        "durable_write:commit#67",
        "durable_write:commit#68",
        "durable_write:commit#69",
        "durable_write:commit#70",
        "durable_write:commit#71",
        "durable_write:commit#72",
        "durable_write:commit#73",
        "executor_request:adopt_revision#1",
        "executor_request:close_attempt_scope#1",
        "executor_request:close_session#1",
        "executor_request:discard_workspace#1",
        "executor_request:ensure_session#1",
        "executor_request:retain_revision#1",
        "executor_request:snapshot_and_retain#1",
        "executor_request:submit_measurement#2",
        "executor_request:verify_adoption#1",
    }
)

_P1_3 = frozenset(
    {
        "durable_write:commit#26",
        "durable_write:commit#27",
    }
)

_REASONS = {
    "P1-1": (
        "P1-1: a turn or job observation sealed before the crash is adopted through "
        "InspectRequest without its owner events (TurnObserved reply, job view), so core "
        "waits for an event nothing will produce (_request_inspection._sealed, reply_owed)"
    ),
    "P1-2": (
        "P1-2: the recovery check of a completed request (EnsureSession, SnapshotAndRetain) "
        "never resolves, so recovery stays RECOVERING and admission is gated until the run "
        "deadline (_intent_recovery._resolution, children_complete of the terminal observation)"
    ),
    "P1-3": (
        "P1-3: a crash between the EnsureSession observation and its SessionObserved owner "
        "event leaves the session unaccepted and the ensure check pending after the restart"
    ),
}


def _known_failure(crossing: Crossing) -> pytest.MarkDecorator | None:
    name = _name(crossing)
    for label, names in (("P1-1", _P1_1), ("P1-2", _P1_2), ("P1-3", _P1_3)):
        if name in names:
            return pytest.mark.xfail(strict=True, reason=_REASONS[label])
    return None


def _points(crossings: tuple[Crossing, ...]) -> list[object]:
    return [
        pytest.param(c, id=_name(c), marks=[m] if (m := _known_failure(c)) else [])
        for c in crossings
    ]


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


@pytest.mark.parametrize("crossing", _points(crash_points()))
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


@pytest.mark.parametrize("crossing", _points(pre_effect_points()))
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
