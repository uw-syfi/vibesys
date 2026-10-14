"""What the search does with an input whose own benchmark fails, round after round.

The input is measured once, before any candidate. When its benchmark fails, that is a permanent
fact of the input revision: every planning turn of the run is told the failure and what the
evaluator printed, the input has no baseline value, and the best candidate wins. How the failure
is classified from the evaluator's evidence (`test_input_measurement.py` in `tests/vibesys/candidates`),
how often the backend measures an ambiguous failure (`libs/vs-runtime`) and that a restarted host
neither measures the input again nor redoes finished work (core's recovery, which `FlowRun` adds
to every story here) are those layers' own tests.
"""

from __future__ import annotations

from tests.vibesys.orchestration.dynamic._evidence_host import (
    EvidenceHost,
    begin_with_evidence,
    finish_workstream,
    plan,
)
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, config
from tests.vibesys.orchestration.dynamic._replies import implement
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.candidates.api import InputFailureKind
from vibesys.orchestration.dynamic.search.api import PlannerPrompt, PromptTemplate
from vs_core.api import Finish, Measure, Operation, RevisionRef, StartAttempt, Wait

MODES = across_restarts
# What a benchmark that stopped itself printed, with a run of backticks that could close a fence.
STOP_TEXT = "warmup stopped: 1/100 rounds ```python"


def _planner_prompt(host: EvidenceHost) -> PlannerPrompt:
    context = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(context, PlannerPrompt)
    return context


def _input_measurements(host: EvidenceHost) -> int:
    """How many times the run asked core to measure the input."""
    return sum(
        isinstance(item, Wait)
        and isinstance(item.request, Measure)
        and item.request.plan.purpose == "baseline"
        for item in host.run.decisions
    )


@MODES
def test_input_benchmark_failure_is_told_to_every_planning_turn_and_the_best_candidate_wins(
    *, restarting: bool
) -> None:
    host = begin_with_evidence(
        config(max_rounds=2),
        restarting=restarting,
        baseline="failed-workload",
        crash_text=STOP_TEXT,
    )
    host.drain_until_turn(PLANNER_LABEL)
    first = _planner_prompt(host)
    plan(host, implement("h1"))
    finish_workstream(host, "h1", "c1")
    host.drain_until_turn(PLANNER_LABEL)
    second = _planner_prompt(host)
    plan(host, implement("h2", parent_hypothesis_id="h1"))
    for asked in host.open(Operation):
        host.answer(asked)  # core checks that h1's revision still reproduces
    (start,) = host.open(StartAttempt)
    assert isinstance(start.request, StartAttempt)
    # h1's finished work is built on, never redone.
    assert start.request.workspace.base == RevisionRef.of_git_commit("c1")
    finish_workstream(host, "h2", "c2")
    host.drain()

    for prompt in (first, second):
        assert prompt.input_failure is not None
        assert prompt.input_failure.kind is InputFailureKind.BENCHMARK_FAILED
        assert prompt.input_failure.output == STOP_TEXT
        assert prompt.baseline == ()
        text = render(PromptTemplate.PORTFOLIO, prompt)
        assert "Input not measurable: the input's own benchmark failed" in text
        # The evaluator's text is shown as data: a run of backticks in it cannot close its fence.
        assert STOP_TEXT.replace("`", "'") in text
    assert _input_measurements(host) == 1
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "success"
    assert finish.result.reason.startswith("adopted")
    assert finish.result.selection is not None
    # The unmeasurable input has no baseline value to beat, so a candidate is adopted.
    assert finish.result.selection.kind == "retained_candidate"
    assert_flow_closed(host)
