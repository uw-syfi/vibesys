"""However the input's measurement ends, the run ends in a bounded number of requests.

The input is measured once before any candidate. Its measurement can succeed, fail because the
input's own workload cannot run (a typed benchmark failure), end without any evidence because the
evaluation infrastructure failed, or be lost. The infrastructure case once made the run ask for
evidence again after every empty answer, forever. Whatever the outcome, the search proceeds on the
planner's turns, and the run ends as a success only when it holds a trusted result: a retained
candidate, or an input that was measured. The planner is asked while the input is measured, so
each story is played with the measurement landing before and after the planner's first reply.
"""

from __future__ import annotations

import pytest
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import PlannerPrompt
from vs_core.api import Finish, Measure, ProposeWinner, Retained, RevisionRef, StartAttempt, Wait

MODES = across_restarts
# A run asks core for a few dozen requests (workspaces, sessions, turns, measurements). Generous,
# but far below a request loop that never ends.
MAX_REQUESTS = 120
MAX_INPUT_MEASUREMENTS = 3


def run_to_its_end(host: Host, *, candidate_found: bool) -> None:
    """Plan one hypothesis; its candidate passes review, or the judge rejects every turn.

    The first plan answers a prompt asked while the input was being measured, and starts at once.
    When the input fails its measurement before that plan arrives, the plan is dropped and the
    planner, asked again with the failure in view, plans the same hypothesis.
    """
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    host.measure_input()
    if not host.open(StartAttempt):
        again = host.run.expect_turn(role=PLANNER_LABEL)
        assert isinstance(again.context, PlannerPrompt)
        assert again.context.input_failure is not None
        host.run.reply_raw(again, plan_reply(implement("h1")))
    host.answer(host.run.expect(StartAttempt, label="start"))
    for _ in range(1 if candidate_found else 2):  # the default bound is two unmeasured turns
        result = ImplementerResult.model_validate_json(implemented())
        host.run.reply(
            host.run.expect_turn(role="implementer"),
            result,
            checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
        )
        host.run.reply(
            host.run.expect_turn(role="judge"),
            ReviewResult.model_validate_json(reviewed(passed=candidate_found)),
        )
    host.drain()


@MODES
@pytest.mark.parametrize("input_first", [True, False])
@pytest.mark.parametrize("candidate_found", [True, False])
@pytest.mark.parametrize("measurement", ["measured", "failed-workload", "no-evidence", "lost"])
def test_every_input_measurement_outcome_ends_the_run_with_its_status(
    *, restarting: bool, measurement: str, candidate_found: bool, input_first: bool
) -> None:
    host = begin(
        config(),
        restarting=restarting,
        input_first=input_first,
        baseline=measurement,  # ty: ignore[invalid-argument-type]
    )
    run_to_its_end(host, candidate_found=candidate_found)

    decisions = host.run.decisions
    assert len(decisions) <= MAX_REQUESTS
    measures = [
        item.request
        for item in decisions
        if isinstance(item, Wait) and isinstance(item.request, Measure)
    ]
    assert sum(item.plan.purpose == "baseline" for item in measures) <= MAX_INPUT_MEASUREMENTS
    finish = host.run.finished
    assert isinstance(finish, Finish)
    proposals = [
        item.request
        for item in decisions
        if isinstance(item, Wait) and isinstance(item.request, ProposeWinner)
    ]
    result = finish.result
    if candidate_found:
        assert result.outcome == "success"
        assert [item.selection.kind for item in proposals] == ["retained_candidate"]
    elif measurement == "measured":
        assert result.outcome == "success"
        assert [item.selection.kind for item in proposals] == ["trusted_baseline"]
    else:
        assert result.outcome == "failure"
        assert result.reason.startswith("no-winner: no trusted result")
        assert proposals == []
    assert_flow_closed(host)
