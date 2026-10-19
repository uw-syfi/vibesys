"""What the search does with an input whose own benchmark fails, round after round.

The input is measured once, before any candidate (the first planning turn may be asked while it is
still being measured). When its benchmark fails, that is a permanent fact of the input revision:
every planning turn after the measurement is told the failure and what the evaluator printed, the
input has no baseline value, and the best candidate wins. How the failure
is classified from the evaluator's evidence (`test_input_measurement.py` in `tests/vibesys/candidates`),
how often the backend measures an ambiguous failure (`libs/vs-runtime`) and that a restarted host
neither measures the input again nor redoes finished work (core's recovery, which `FlowRun` adds
to every story here) are those layers' own tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.real_git import NO_GIT, NO_GITHUB
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

from headless.framework_events import format_framework_event
from vibesys.candidates.api import InputFailureKind
from vibesys.dynamic_core import dynamic_projector
from vibesys.events import CoreEvent, CoreEventType
from vibesys.orchestration.dynamic.search.api import (
    PlannerCorrection,
    PlannerPrompt,
    PromptTemplate,
    RunSnapshot,
)
from vibesys.run.contracts import RunStatus, RunView
from vibesys.run.integration import LocalRunIntegration
from vs_core.api import FailureReason, Finish, Measure, Operation, RevisionRef, StartAttempt, Wait
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pydantic import BaseModel

MODES = across_restarts
# What a benchmark that stopped itself printed, with a run of backticks that could close a fence.
STOP_TEXT = "warmup stopped: 1/100 rounds ```python"


def _planner_prompt(host: EvidenceHost) -> PlannerPrompt | PlannerCorrection:
    context = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(context, PlannerPrompt | PlannerCorrection)
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
def test_input_benchmark_failure_is_told_to_every_later_planning_turn_and_the_best_candidate_wins(
    *, restarting: bool
) -> None:
    host = begin_with_evidence(
        config(max_rounds=2),
        restarting=restarting,
        baseline="failed-workload",
        crash_text=STOP_TEXT,
    )
    host.drain_until_turn(PLANNER_LABEL)
    pending = _planner_prompt(host)
    host.run.fail(host.run.expect_turn(role=PLANNER_LABEL), FailureReason.TIMED_OUT)
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

    assert pending.baseline_pending
    assert pending.input_failure is None
    for prompt in (first, second):
        assert not prompt.baseline_pending
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


# What the live run's input printed when its accuracy stage hung and hit its time limit (S173).
HANG_TEXT = "accuracy stage exceeded its 1200 s limit with no output; API_TOKEN=s3cret"


class _ViewOfTheRun:
    """Hands the commit observer the product view of the dynamic run's published record."""

    def __init__(self, view: RunView) -> None:
        self.view = view

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        if namespace != "dynamic" or run_id != self.view.run_id:
            return None
        return self.view if isinstance(state, RunSnapshot) else None


def _events_of(snapshot: RunSnapshot) -> list[CoreEvent]:
    """The semantic events the product emits when it commits a run that published ``snapshot``."""
    projection = dynamic_projector().project(snapshot, 1)
    view = RunView(
        run_id="run",
        loop="dynamic",
        status=RunStatus.ACTIVE,
        iterations=projection.iterations,
        rounds=projection.rounds,
        experiment_revision=projection.experiment_revision,
        input_failure=projection.input_failure,
    )
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=NO_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    captured: list[CoreEvent] = []
    integration.events.subscribe(captured.append)
    observer = integration.core_commit_observer("run", _ViewOfTheRun(view), "dynamic")
    observer.committed(None, snapshot)
    observer.committed(None, snapshot)  # a later commit of the same fact is not news
    return captured


def _candidate_measurements(host: EvidenceHost) -> list[str]:
    """The revisions the run asked core to measure as candidates (not the input), in order."""
    return [
        item.request.plan.candidate.revision_id.root
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Measure)
        and item.request.plan.purpose == "official"
        and isinstance(item.request.plan.candidate, RevisionRef)
    ]


@pytest.mark.regression
@MODES
def test_an_input_that_fails_its_accuracy_check_is_announced_told_to_the_planner_and_fixable(
    *, restarting: bool
) -> None:
    """S173: the input's accuracy stage hit its time limit; nobody was told for 1.5 hours.

    The operator gets one event with the failed stage and a redacted diagnostic, the planner's
    next turn is told the input failed and why, and a candidate that passes its own trusted
    evaluation is measured and adopted: with no input value to beat, it is eligible on the
    task's gates alone (the planner's way to fix the input).
    """
    host = begin_with_evidence(
        config(max_rounds=1),
        restarting=restarting,
        baseline="failed-accuracy",
        crash_text=HANG_TEXT,
    )
    host.drain_until_turn(PLANNER_LABEL)
    told = _planner_prompt(host)
    if told.baseline_pending:  # a turn asked while the input is measured plans blind: skip it
        host.run.fail(host.run.expect_turn(role=PLANNER_LABEL), FailureReason.TIMED_OUT)
        told = _planner_prompt(host)
    assert not told.baseline_pending
    snapshot = host.run.published(RunSnapshot)
    assert snapshot is not None
    plan(host, implement("h1"))
    finish_workstream(host, "h1", "c1")
    host.drain()

    # (1) The operator sees it at once: one event, the failed stage, a redacted diagnostic.
    failed = [
        event
        for event in _events_of(snapshot)
        if event.type is CoreEventType.INPUT_MEASUREMENT_FAILED
    ]
    assert len(failed) == 1
    data = failed[0].data
    assert data is not None
    assert data.kind == "input_measurement_failed"
    assert data.failure == "accuracy-failed"
    assert data.stage == "correctness"
    assert "exceeded its 1200 s limit" in data.summary
    assert "s3cret" not in data.summary
    assert format_framework_event(failed[0]) is not None

    # (2) The planner's next turn is told the input failed, where, and what it printed.
    failure = told.input_failure
    assert failure is not None
    assert failure.kind is InputFailureKind.ACCURACY_FAILED
    assert failure.stage == "correctness"
    assert failure.output == HANG_TEXT
    text = render(PromptTemplate.PORTFOLIO, told)
    assert "Input not measurable: the input failed its trusted accuracy check" in text
    assert "stage `correctness`" in text
    assert HANG_TEXT in text

    # (3) Candidates are still measured (absolutely: there is no input value to compare
    # with) and one that passes its gates is adopted.
    assert _candidate_measurements(host) == ["c1"]
    assert _input_measurements(host) == 1
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "success"
    assert finish.result.selection is not None
    assert finish.result.selection.kind == "retained_candidate"
    assert_flow_closed(host)
