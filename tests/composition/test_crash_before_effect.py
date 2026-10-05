"""A host crash between authorizing a request and running it.

The restart inspects the request. If it never began (the receipt store has no ``begun``
record) core issues the same request again, once per recovery epoch, without asking the
strategy to replan. If it began and has no result (the crash came right after the ``begun``
marker), core issues it again to resume, and the executor inspects the external effect before
repeating anything. Recovery is replay, so the run ends exactly as the straight run does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import crash_plan, name, pre_effect_points, run, straight_run

from vs_core.api import RunStatus

if TYPE_CHECKING:
    from vs_faults.api import Crossing


# A turn that crashed after its begun record is inspected as a turn, and core rejects that
# inspection (no canonical invocation owner exists before the turn's first observation). What
# an interrupted turn becomes is turn policy (LIVE-ROBUST-B), so the gap is tracked there.
_TURN_GAP = frozenset({"durable_write:receipt_begun#5"})


def _points() -> list[object]:
    return [
        pytest.param(
            c,
            id=name(c),
            marks=[
                pytest.mark.xfail(
                    strict=True,
                    reason="core rejects the inspection of a turn that never produced an observation",
                )
            ]
            if name(c) in _TURN_GAP
            else [],
        )
        for c in pre_effect_points()
    ]


@pytest.mark.parametrize("crossing", _points())
def test_a_crash_before_an_effect_ends_the_run_as_the_straight_run_does(
    crossing: Crossing,
) -> None:
    plan = crash_plan(crossing)
    summary = run(plan).summary
    straight = straight_run().summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.status == RunStatus.TERMINAL, replay
    assert summary.crashes == 1, replay
    assert summary.outcome == straight.outcome, replay
    assert summary.adopted_tree == straight.adopted_tree, replay
    assert summary.sbatch_calls == straight.sbatch_calls, replay
    assert summary.agent_dispatches == straight.agent_dispatches, replay
