"""A second host crash while the restarted host is still recovering."""

from __future__ import annotations

from functools import cache

import pytest
from tests.support.crash_harness import (
    crash_plan,
    crash_points,
    name,
    rule,
    run,
    straight_run,
)

from vs_faults.api import Boundary, Crossing, FaultPlan

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
    calls = run(crash_plan(first)).gate.calls
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
            id=f"{name(first)}+{name(second)}",
            marks=marks if f"{name(first)}+{name(second)}" in _REISSUED_POLL else [],
        )
        for first in _first_of_each_kind()
        for second in _sampled(_recovery_window(first))
    ]


@pytest.mark.parametrize(("first", "second"), _double_crashes())
def test_a_crash_during_recovery_still_converges(first: Crossing, second: Crossing) -> None:
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
