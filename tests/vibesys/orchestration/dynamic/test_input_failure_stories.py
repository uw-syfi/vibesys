"""What the search does with an input whose own measurement fails, round after round.

The input is measured before any candidate (the first planning turn may be asked while it is
still being measured). A failure that shows no fault of the input (no evidence, a wait core lost
or timed out, a stage the backend did not blame on the input, or one that printed nothing) is
measured once more; any other failure is final. A final failure is a fact of the input revision:
every planning turn after it is told the failure and what the evaluator printed. If the input's
benchmark still produced numbers, they stay the bar a candidate must beat; with no bar, a
candidate that passes its gates is adopted, and the run ends "adopted without an input
comparison", never as a plain adoption. How the failure is classified from the evaluator's
evidence (`test_input_measurement.py` in `tests/vibesys/candidates`) and that a restarted host
neither measures the input again nor redoes finished work (core's recovery, which `FlowRun` adds
to every story here) are those layers' own tests.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from tests.support.real_git import NO_GIT, NO_GITHUB
from tests.vibesys.orchestration.dynamic._evidence_host import (
    EvidenceHost,
    begin_with_evidence,
    finish_workstream,
    plan,
)
from tests.vibesys.orchestration.dynamic._host import INPUT_THROUGHPUT, PLANNER_LABEL, config
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
from vs_core.api import (
    EndReason,
    FailureReason,
    Finish,
    Measure,
    MeasurementFailure,
    Operation,
    RevisionRef,
    StartAttempt,
    Wait,
)
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
def test_input_benchmark_failure_is_told_to_every_later_planning_turn_and_ends_uncompared(
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
        assert '"adopted without an input comparison"' in text
        # The input printed why it failed on its own: fixing that is worth planning.
        assert "Plan the next work to fix what made the input fail" in text
        # The evaluator's text is shown as data: a run of backticks in it cannot close its fence.
        assert STOP_TEXT.replace("`", "'") in text
    # The input's benchmark failed and said why: that is final, so it is not measured again.
    assert _input_measurements(host) == 1
    _assert_adopted(host, uncompared=True)
    assert_flow_closed(host)


def _assert_adopted(host: EvidenceHost, *, uncompared: bool) -> None:
    """The run adopted a candidate, said to be uncompared exactly when the input had no bar."""
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "success"
    assert finish.result.selection is not None
    assert finish.result.selection.kind == "retained_candidate"
    told = finish.result.reason.split(":", 1)[0]
    assert told == (
        EndReason.ADOPTED_WITHOUT_INPUT_COMPARISON if uncompared else EndReason.ADOPTED
    ), finish.result.reason


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

    A stage's time limit is no evidence against the input (the backend classifies it ambiguous),
    so the input is measured once more; it fails the same way. The operator gets one event with
    the failed stage, both attempts and a redacted diagnostic, the planner's next turn is told the
    input failed, why, and that its benchmark's numbers stay the bar, and a candidate that passes
    its own trusted evaluation and beats them is adopted.
    """
    host = begin_with_evidence(
        config(max_rounds=1),
        restarting=restarting,
        baseline="failed-accuracy",
        crash_text=HANG_TEXT,
        input_blame=MeasurementFailure.AMBIGUOUS,
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
    assert (data.attempts, data.compared) == (2, True)
    assert "exceeded its 1200 s limit" in data.summary
    assert "s3cret" not in data.summary
    assert format_framework_event(failed[0]) is not None

    # (2) The planner's next turn is told the input failed, where, and what it printed.
    failure = told.input_failure
    assert failure is not None
    assert failure.kind is InputFailureKind.ACCURACY_FAILED
    assert failure.stage == "correctness"
    assert failure.output == HANG_TEXT
    assert told.input_first_attempt is not None
    assert told.input_first_attempt.kind is InputFailureKind.ACCURACY_FAILED
    assert not told.input_at_fault
    assert [row.value for row in told.baseline] == [INPUT_THROUGHPUT]
    text = render(PromptTemplate.PORTFOLIO, told)
    assert "Input not measurable: the input failed its trusted accuracy check" in text
    assert "stage `correctness`" in text
    assert "The input was measured twice" in text
    assert "its numbers above stay the bar" in text
    assert "Nothing shows a fault in the input's code" in text
    assert HANG_TEXT in text

    # (3) The input was measured twice, and the candidate was compared with its benchmark's
    # numbers and adopted as a plain adoption.
    assert _candidate_measurements(host) == ["c1"]
    assert _input_measurements(host) == 2
    _assert_adopted(host, uncompared=False)
    assert_flow_closed(host)


@pytest.mark.regression
@MODES
def test_an_input_measurement_lost_twice_is_announced_and_the_run_ends_uncompared(
    *, restarting: bool
) -> None:
    """A lost measurement shows nothing about the input: it is taken once more, then reported.

    Both attempts are lost, so there is no bar. The operator is told (two attempts, uncompared),
    the planner is told not to plan work only to fix the input, and the candidate that passes its
    gates is adopted without an input comparison.
    """
    host = begin_with_evidence(config(max_rounds=1), restarting=restarting, baseline="lost")
    host.drain_until_turn(PLANNER_LABEL)
    told = _planner_prompt(host)
    if told.baseline_pending:
        host.run.fail(host.run.expect_turn(role=PLANNER_LABEL), FailureReason.TIMED_OUT)
        told = _planner_prompt(host)
    snapshot = host.run.published(RunSnapshot)
    assert snapshot is not None
    plan(host, implement("h1"))
    finish_workstream(host, "h1", "c1")
    host.drain()

    (event,) = [
        event
        for event in _events_of(snapshot)
        if event.type is CoreEventType.INPUT_MEASUREMENT_FAILED
    ]
    assert event.data is not None
    assert event.data.kind == "input_measurement_failed"
    assert event.data.failure == "wait-ended"
    assert (event.data.attempts, event.data.compared) == (2, False)
    assert told.input_failure is not None
    assert told.input_failure.kind is InputFailureKind.WAIT_ENDED
    assert told.input_first_attempt is not None
    assert not told.input_at_fault
    assert told.baseline == ()
    text = render(PromptTemplate.PORTFOLIO, told)
    assert '"adopted without an input comparison"' in text
    assert "Do not plan work only to fix the" in text
    assert _input_measurements(host) == host.input_measurements == 2
    _assert_adopted(host, uncompared=True)
    assert_flow_closed(host)


@MODES
def test_an_input_measurement_lost_once_is_taken_again_and_the_run_compares_with_it(
    *, restarting: bool
) -> None:
    """The second measurement succeeds: no failure is told and candidates are compared as usual."""
    host = begin_with_evidence(
        config(max_rounds=1), restarting=restarting, baseline="lost", input_recovers=True
    )
    host.drain_until_turn(PLANNER_LABEL)
    told = _planner_prompt(host)
    if told.baseline_pending:
        host.run.fail(host.run.expect_turn(role=PLANNER_LABEL), FailureReason.TIMED_OUT)
        told = _planner_prompt(host)
    snapshot = host.run.published(RunSnapshot)
    assert snapshot is not None
    plan(host, implement("h1"))
    finish_workstream(host, "h1", "c1")
    host.drain()

    assert not [
        event
        for event in _events_of(snapshot)
        if event.type is CoreEventType.INPUT_MEASUREMENT_FAILED
    ]
    assert told.input_failure is None
    assert [row.value for row in told.baseline] == [INPUT_THROUGHPUT]
    assert _input_measurements(host) == host.input_measurements == 2
    _assert_adopted(host, uncompared=False)
    assert_flow_closed(host)


# How the version before S173 recorded an input that failed its accuracy check: a measured input
# that said so. Such a run resumes on this build.
_PRE_S173_ACCURACY_FAILED = {
    "kind": "measured",
    "reading": {
        "accuracy_passed": False,
        "benchmark_passed": True,
        "metrics": [{"name": "throughput", "value": 50.0, "unit": "tokens/s", "direction": "max"}],
        "partial": None,
    },
}


@pytest.mark.regression
def test_a_run_checkpointed_before_s173_with_a_failed_accuracy_check_announces_it_on_resume() -> (
    None
):
    """F4: the old record loads as the accuracy failure it was, so the resumed run tells it.

    The resumed record's projection carries the failure, with its benchmark still the bar, and
    the commit observer of the resumed process announces it once.
    """
    host = begin_with_evidence(config(max_rounds=1), restarting=False)
    host.drain_until_turn(PLANNER_LABEL)
    published = host.run.published(RunSnapshot)
    assert published is not None
    written = published.model_dump(mode="json")
    written["baseline"] = _PRE_S173_ACCURACY_FAILED
    resumed = RunSnapshot.model_validate_json(json.dumps(written))

    (event,) = [
        event
        for event in _events_of(resumed)
        if event.type is CoreEventType.INPUT_MEASUREMENT_FAILED
    ]
    assert event.data is not None
    assert event.data.kind == "input_measurement_failed"
    assert event.data.failure == "accuracy-failed"
    assert event.data.stage == "accuracy"
    assert (event.data.attempts, event.data.compared) == (1, True)
