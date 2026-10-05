"""A host crash between authorizing a request and running it.

The restart inspects the request, finds it never began (the receipt store has no ``begun``
record) and core issues the same request again, once per recovery epoch, without asking the
strategy to replan. Recovery is replay, so the run ends exactly as the straight run does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import crash_plan, name, pre_effect_points, run, straight_run

from vs_core.api import RunStatus

if TYPE_CHECKING:
    from vs_faults.api import Crossing


@pytest.mark.parametrize("crossing", pre_effect_points(), ids=name)
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
