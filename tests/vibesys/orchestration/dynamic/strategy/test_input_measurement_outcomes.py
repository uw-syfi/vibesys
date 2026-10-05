"""However the input measurement ends, the run ends in a bounded number of core requests.

The input is measured once before any candidate. Its measurement can succeed, fail
because the input's own workload cannot run (a typed benchmark failure), or end
without any evidence because the evaluation infrastructure failed. The last case once
made the run ask for evidence again after every empty answer, forever. Whatever the
outcome, the search proceeds on the planner's turns, and the run ends as a success only
when it holds a trusted result: a retained candidate, or an input that was measured.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell

from vs_core.api import CollectEvidence, ProposeWinner, Stop

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


def _executors(measurement: Input, *, candidate_found: bool) -> Executors:
    executors = Executors(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    if measurement is Input.FAILED_WORKLOAD:
        executors.baseline_value = None
    if measurement is Input.INFRASTRUCTURE:
        executors.infrastructure_failure = lambda plan: plan.purpose == "baseline"
    if candidate_found:
        executors.benchmark = lambda _commit: 100.0
    else:
        executors.benchmark = lambda _commit: 10.0 if measurement is Input.MEASURED else None
    return executors


def _stop(decisions: Sequence[Decision]) -> RunResultProposal:
    final = decisions[-1]
    assert isinstance(final, Stop), "the run never reached a terminal decision"
    return final.result


@pytest.mark.parametrize("candidate_found", [True, False])
@pytest.mark.parametrize("measurement", list(Input))
def test_every_input_measurement_outcome_ends_the_run_with_its_status(
    measurement: Input, *, candidate_found: bool
) -> None:
    executors = _executors(measurement, candidate_found=candidate_found)

    trace = run_shell(executors)

    assert len(executors.seen) <= MAX_REQUESTS
    assert sum(isinstance(request, CollectEvidence) for request in executors.seen) <= 3
    result = _stop(trace.decisions)
    proposals = [item for item in trace.decisions if isinstance(item, ProposeWinner)]
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
