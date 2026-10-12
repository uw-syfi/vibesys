"""However the input measurement ends, the run ends in a bounded number of core requests.

The input is measured once before any candidate. Its measurement can succeed, fail
because the input's own workload cannot run (a typed benchmark failure), or end
without any evidence because the evaluation infrastructure failed. The last case once
made the run ask for evidence again after every empty answer, forever. Whatever the
outcome, the search proceeds on the planner's turns, and the run ends as a success only
when it holds a trusted result: a retained candidate, or an input that was measured.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vs_core.api import CollectEvidence, ProposeWinner, Stop
from vs_dst.api import simulate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import Decision, RunResultProposal

# A run asks core for a few dozen requests (workspaces, sessions, turns, measurements).
# Generous, but far below a request loop that never ends.
MAX_REQUESTS = 120


class Input(StrEnum):
    MEASURED = "measured"
    FAILED_WORKLOAD = "failed_workload"
    INFRASTRUCTURE = "infrastructure"


def _world(measurement: Input, *, candidate_found: bool) -> DynamicWorld:
    loser = 10.0 if measurement is Input.MEASURED else None
    candidate = 100.0 if candidate_found else loser
    return DynamicWorld(
        baseline_throughput=None if measurement is Input.FAILED_WORKLOAD else 50.0,
        unmeasured=("baseline",) if measurement is Input.INFRASTRUCTURE else (),
        throughputs=(candidate,),
    )


def _stop(decisions: Sequence[Decision]) -> RunResultProposal:
    final = decisions[-1]
    assert isinstance(final, Stop), "the run never reached a terminal decision"
    return final.result


@pytest.mark.parametrize("candidate_found", [True, False])
@pytest.mark.parametrize("measurement", list(Input))
def test_every_input_measurement_outcome_ends_the_run_with_its_status(
    measurement: Input, *, candidate_found: bool
) -> None:
    run = simulate(_world(measurement, candidate_found=candidate_found))

    assert run.finished
    assert not run.liveness_violations
    requests = [intent.request for intent in run.core.intents.intents]
    assert len(requests) <= MAX_REQUESTS
    assert sum(isinstance(request, CollectEvidence) for request in requests) <= 3
    result = _stop(run.decisions)
    proposals = [item for item in run.decisions if isinstance(item, ProposeWinner)]
    if candidate_found:
        assert result.outcome == "success"
        assert [item.selection.kind for item in proposals] == ["retained_candidate"]
    elif measurement is Input.MEASURED:
        assert result.outcome == "success"
        assert [item.selection.kind for item in proposals] == ["trusted_baseline"]
    else:
        assert result.outcome == "failure"
        assert result.reason.startswith("no trusted result")
        assert proposals == []
