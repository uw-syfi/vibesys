"""A host crash between authorizing a request and running it.

The restart inspects the request, finds it never began and reports it REJECTED (by design,
#1342). Two properties follow: no effect that did run repeats (holds), and the run still
reaches a terminal status (does not: nothing re-issues the rejected effect, so the run idles
or, for a CloseSession, leaves its session CLOSING).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import crash_plan, name, pre_effect_points, run, straight_run

from vs_core.api import RunStatus

if TYPE_CHECKING:
    from vs_faults.api import Crossing


# The one point where the no-orphan-waits check itself fails the run: a CloseSession that never
# began leaves its session CLOSING with nothing to end the wait.
_ORPHANED = frozenset({"durable_write:commit#52"})


def _points() -> list[object]:
    return [
        pytest.param(
            c,
            id=name(c),
            marks=[
                pytest.mark.xfail(
                    strict=True, reason="a never-started CloseSession orphans its session"
                )
            ]
            if name(c) in _ORPHANED
            else [],
        )
        for c in pre_effect_points()
    ]


@pytest.mark.parametrize("crossing", _points())
def test_a_crash_before_an_effect_never_repeats_one(crossing: Crossing) -> None:
    plan = crash_plan(crossing)
    summary = run(plan).summary
    straight = straight_run().summary
    replay = f"replay with {plan.model_dump_json()}"
    assert max(summary.sbatch_calls, default=0) <= 1, replay
    assert max((n for _, n in summary.agent_dispatches), default=0) <= 1, replay
    assert len(summary.sbatch_calls) <= len(straight.sbatch_calls), replay


@pytest.mark.xfail(
    strict=True,
    reason="a never-started effect is rejected and nothing re-issues it, so the run stalls",
)
@pytest.mark.parametrize("crossing", pre_effect_points(), ids=name)
def test_a_crash_before_an_effect_still_ends_the_run(crossing: Crossing) -> None:
    plan = crash_plan(crossing)
    summary = run(plan).summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.status == RunStatus.TERMINAL, replay
