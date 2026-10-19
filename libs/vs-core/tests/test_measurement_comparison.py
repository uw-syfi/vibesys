"""A candidate is compared with its base from the samples of both, and the base is cached (#3047).

Every conclusive framework run of a measured input is a sample of one measurement. An agent's
evaluation of a candidate also gets the base measured, once per (measured input, evaluation),
unless fresh samples of it are held or being taken; core compares the candidate's samples with
the base's and records the comparison. These tests drive the kernel through its public events.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_measurement_reuse import (
    Variant,
    measured,
    reloaded,
    revision,
    seen,
    submissions,
)
from .test_measurements import committed, plan, transition

if TYPE_CHECKING:
    import random

METRIC = "tok_s"
FLOOR = 0.02
STALENESS = 600.0
_PASSED = core.EvaluationTerminalFacts(
    stages=(
        core.EvaluationStageResult(stage_id="accuracy", outcome=core.EvaluationStageOutcome.PASSED),
        core.EvaluationStageResult(
            stage_id="benchmark", outcome=core.EvaluationStageOutcome.PASSED
        ),
    ),
    accuracy_passed=True,
)
_FAILED = core.EvaluationTerminalFacts(
    stages=(
        core.EvaluationStageResult(stage_id="accuracy", outcome=core.EvaluationStageOutcome.PASSED),
        core.EvaluationStageResult(
            stage_id="benchmark", outcome=core.EvaluationStageOutcome.FAILED
        ),
    ),
    accuracy_passed=True,
)


def terms(*, floor: float = FLOOR, staleness: float = STALENESS) -> core.TaskTerms:
    return core.TaskTerms(
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        stages=(
            core.TaskStage(stage_id="accuracy", execution_seconds=20.0),
            core.TaskStage(stage_id="benchmark", execution_seconds=70.0),
        ),
        measured=("accuracy", "benchmark"),
        benchmark_stage="benchmark",
        queue_allowance=10.0,
        operation_seconds=10.0,
        objectives=(core.TaskObjective(name=METRIC, direction="max"),),
        relative_noise=floor,
        base_staleness=staleness,
    )


def attempt(
    name: str, base: core.RevisionRef, state: core.CoreState, *, base_input: str | None = "base"
) -> tuple[core.CoreState, core.Scope]:
    """``state`` with one more active attempt whose workspace was opened on ``base``.

    ``base_input`` is what the runtime digested at ``base`` when it ensured the workspace; None
    when it reported none.
    """
    owner = core.AttemptView(
        attempt_id=core.AttemptId(root=name),
        item_id=core.ItemId(root=f"item-{name}"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=base),
        base_input=base_input,
        budget=core.AttemptBudget(),
        admission_id=core.DecisionId(root=f"admission-{name}"),
    )
    attempts = core.AttemptsState(attempts=(*state.attempts.attempts, owner))
    return state.model_copy(update={"attempts": attempts}), core.Scope(
        owner=owner.attempt_id, generation=0
    )


def stamped(
    state: core.CoreState, *, now: float | None = None, **terms_fields: float
) -> core.CoreState:
    """``state`` with the task's terms (and the run clock) set."""
    facts = state.run.facts.model_copy(update={"task": terms(**terms_fields)})
    run = state.run.model_copy(
        update={
            "facts": facts,
            "now_at": state.run.now_at if now is None else now,
            "deadline_at": 1_000_000.0,
        }
    )
    return state.model_copy(update={"run": run})


def reading(value: float) -> core.Reading:
    return core.Reading(metric=METRIC, value=value, direction="max", unit="tok/s")


def started_with(state: core.CoreState, request: core.SubmitMeasurement) -> core.CoreState:
    """Accept ``request``'s job: it is running, and core holds it in the order it started."""
    return started_steps(state, request)[-1].state


def started_steps(
    state: core.CoreState, request: core.SubmitMeasurement
) -> tuple[core.Transition, ...]:
    """Every transition `started_with` takes, in order."""
    started = seen(request, 1)
    state = committed(state, started)
    observed = transition(state, core.MeasurementSubmissionObserved(observation=started))
    resource = started.resource_id
    assert resource is not None
    return (
        observed,
        transition(observed.state, core.JobObserved(resource_id=resource, observation=started)),
    )


def ended_with(
    state: core.CoreState,
    request: core.SubmitMeasurement,
    value: float | None,
    *,
    reused: core.RequestId | None = None,
    failed: bool = False,
) -> core.CoreState:
    """Accept ``request``'s job and end it with the benchmark reading ``value``."""
    return ended_transition(state, request, value, reused=reused, failed=failed).state


def ended_transition(
    state: core.CoreState,
    request: core.SubmitMeasurement,
    value: float | None,
    *,
    reused: core.RequestId | None = None,
    failed: bool = False,
) -> core.Transition:
    """End a newly accepted job and return its final transition."""
    state = started_with(state, request)
    return finished_transition(state, request, value, reused=reused, failed=failed)


def finished_with(
    state: core.CoreState,
    request: core.SubmitMeasurement,
    value: float | None,
    *,
    reused: core.RequestId | None = None,
    failed: bool = False,
) -> core.CoreState:
    """End an already accepted job as passed or as a workload failure."""
    return finished_transition(state, request, value, reused=reused, failed=failed).state


def finished_transition(
    state: core.CoreState,
    request: core.SubmitMeasurement,
    value: float | None,
    *,
    reused: core.RequestId | None = None,
    failed: bool = False,
) -> core.Transition:
    """End an already accepted job and return the final transition."""
    return finished_steps(state, request, value, reused=reused, failed=failed)[-1]


def ended_steps(
    state: core.CoreState, request: core.SubmitMeasurement, value: float | None
) -> tuple[core.Transition, ...]:
    """Every transition `ended_transition` takes, in order."""
    started = started_steps(state, request)
    return (*started, *finished_steps(started[-1].state, request, value))


def finished_steps(
    state: core.CoreState,
    request: core.SubmitMeasurement,
    value: float | None,
    *,
    reused: core.RequestId | None = None,
    failed: bool = False,
) -> tuple[core.Transition, ...]:
    """Every transition `finished_transition` takes, in order."""
    status = core.ObservationStatus.FAILED if failed else core.ObservationStatus.SUCCEEDED
    finished = seen(
        request,
        2,
        status=status,
        terminal=True,
        released=True,
        children_complete=True,
        observed_at=state.run.now_at,
    )
    resource = finished.resource_id
    assert resource is not None
    assert isinstance(request.plan.candidate, core.RevisionRef)
    assert request.request_id is not None
    source = request.request_id if reused is None else reused
    records = tuple(
        core.EvidenceRef(
            evidence_id=core.EvidenceId(root=f"{source.root}-{stage}"),
            kind=core.EvidenceKind.CORRECTNESS
            if stage == "accuracy"
            else core.EvidenceKind.BENCHMARK,
            purpose=request.plan.purpose,
            scope=request.scope,
            source_request=finished.request_id,
            candidate=request.plan.candidate,
            observation_sequence=finished.sequence,
            evaluator_digest=request.plan.evaluator_digest,
            workload_digest=request.plan.workload_digest,
            environment_digest=request.plan.environment_digest,
            provenance="trusted",
            status=status,
            reading=reading(value) if stage == "benchmark" and value is not None else None,
            reused_from=reused,
        )
        for stage in ("accuracy", "benchmark")
    )
    facts = _FAILED if failed else _PASSED
    failure = core.MeasurementFailure.WORKLOAD if failed else None
    state = committed(state, finished, facts)
    observed = transition(
        state, core.MeasurementSubmissionObserved(observation=finished, failure=failure)
    )
    return (
        observed,
        transition(
            observed.state,
            core.JobObserved(
                resource_id=resource,
                observation=finished,
                evidence=records,
                evaluation_result=facts,
                failure=failure,
            ),
        ),
    )


@dataclass
class Lab:
    """One run with attempts, driven only through the agent's evaluation call and job ends.

    The base's runs are told apart from the candidates' by their plan's purpose. ``measure``
    ends every job the lab has not ended yet at the value its input measures.
    """

    state: core.CoreState
    scopes: dict[str, core.Scope] = field(default_factory=dict)
    calls: int = 0
    ended: set[str] = field(default_factory=set)

    @classmethod
    def of(
        cls,
        *attempts: str,
        floor: float = FLOOR,
        staleness: float = STALENESS,
        base_input: str | None = "base",
    ) -> Lab:
        state = stamped(core.initial_state(), floor=floor, staleness=staleness)
        scopes: dict[str, core.Scope] = {}
        for name in attempts:
            state, scopes[name] = attempt(
                name, state.run.facts.baseline, state, base_input=base_input
            )
        return cls(state=state, scopes=scopes)

    def call(
        self,
        name: str,
        commit: str,
        candidate_input: str,
        *,
        variant: Variant = "same",
    ) -> list[core.SubmitMeasurement]:
        """The agent of attempt ``name`` evaluates ``commit``; the submissions core allocated.

        ``variant`` is how the plan differs from the official one: another variant is another
        evaluation, whose base is measured under it too.
        """
        before = len(submissions(self.state))
        self.calls += 1
        self.state = transition(
            self.state,
            core.AgentMeasurementRequested(
                scope=self.scopes[name],
                plan=measured("local-validation", commit, candidate_input, variant),
                within=core.Within(limit=100.0),
                call_id=f"call-{self.calls}",
            ),
        ).state
        return submissions(self.state)[before:]

    def measure(self, values: dict[str, float]) -> None:
        """End every job still running, at the value its measured input has in ``values``."""
        while True:
            running = [
                request
                for request in submissions(self.state)
                if request.request_id is not None and request.request_id.root not in self.ended
            ]
            if not running:
                return
            request = running[0]
            assert request.request_id is not None
            self.ended.add(request.request_id.root)
            assert request.plan.measured_input is not None
            self.state = ended_with(self.state, request, values[request.plan.measured_input])

    def advance(self, seconds: float) -> None:
        """Move the run clock on."""
        run = self.state.run.model_copy(update={"now_at": self.state.run.now_at + seconds})
        self.state = self.state.model_copy(update={"run": run})

    def baselines(self) -> list[core.SubmitMeasurement]:
        return [r for r in submissions(self.state) if r.plan.purpose == "baseline"]

    def comparisons(self) -> tuple[core.Comparison, ...]:
        return self.state.evaluation.comparisons


def test_a_candidate_run_asks_for_one_run_of_its_base_beside_its_own() -> None:
    lab = Lab.of("a")
    asked = lab.call("a", "c1", "input-1")
    assert [r.plan.purpose for r in asked] == ["local-validation", "baseline"]
    candidate, base = asked
    assert candidate.base is not None
    assert candidate.base.commit == core.initial_state().run.facts.baseline
    assert candidate.base.measured_input == "base"
    assert base.plan.candidate == candidate.base.commit
    assert base.plan.measured_input == "base"
    assert base.plan.stages == candidate.plan.stages


def test_a_task_that_names_no_benchmark_stage_measures_no_base() -> None:
    lab = Lab.of("a")
    task = lab.state.run.facts.task
    assert task is not None
    facts = lab.state.run.facts.model_copy(
        update={"task": task.model_copy(update={"benchmark_stage": None})}
    )
    lab.state = lab.state.model_copy(
        update={"run": lab.state.run.model_copy(update={"facts": facts})}
    )
    asked = lab.call("a", "c1", "input-1")
    assert [r.plan.purpose for r in asked] == ["local-validation"]


def test_a_base_run_that_gave_no_sample_is_not_asked_for_again() -> None:
    """A benchmark that reports no metric cannot give a sample: one probe, never a loop."""
    lab = Lab.of("a")
    asked = lab.call("a", "c1", "input-1")
    assert [r.plan.purpose for r in asked] == ["local-validation", "baseline"]
    for request in asked:
        assert request.request_id is not None
        lab.ended.add(request.request_id.root)
        lab.state = ended_with(lab.state, request, None)
    assert lab.state.evaluation.samples == ()
    for number in range(2, 5):
        asked = lab.call("a", f"c{number}", f"input-{number}")
        assert [r.plan.purpose for r in asked] == ["local-validation"], number


def test_a_call_without_the_bases_measured_input_is_not_compared() -> None:
    lab = Lab.of("a", base_input=None)
    lab.state = transition(
        lab.state,
        core.AgentMeasurementRequested(
            scope=lab.scopes["a"],
            plan=measured("local-validation", "c1", "input-1"),
            within=core.Within(limit=100.0),
            call_id="c",
        ),
    ).state
    assert [r.plan.purpose for r in submissions(lab.state)] == ["local-validation"]


def test_a_candidate_without_a_measured_input_has_nothing_to_compare() -> None:
    lab = Lab.of("a")
    lab.state = transition(
        lab.state,
        core.AgentMeasurementRequested(
            scope=lab.scopes["a"],
            plan=measured("local-validation", "c1", None),
            within=core.Within(limit=100.0),
            call_id="c",
        ),
    ).state
    assert [r.plan.purpose for r in submissions(lab.state)] == ["local-validation"]


def test_a_comparison_is_made_once_both_sides_have_a_sample_whichever_ends_first() -> None:
    for order in ("candidate first", "base first"):
        lab = Lab.of("a")
        candidate, base = lab.call("a", "c1", "input-1")
        first, second = (candidate, base) if order == "candidate first" else (base, candidate)
        values = {"input-1": 110.0, "base": 100.0}
        for request in (first, second):
            assert request.request_id is not None
            lab.ended.add(request.request_id.root)
            assert request.plan.measured_input is not None
            lab.state = ended_with(lab.state, request, values[request.plan.measured_input])
            if request is first:
                assert lab.comparisons() == ()
        (comparison,) = lab.comparisons()
        assert comparison.verdict is core.ComparisonVerdict.BETTER, order
        assert comparison.ratio == 1.1
        assert (comparison.candidate_samples, comparison.base_samples) == (1, 1)


def test_the_comparison_names_the_base_commit_and_both_values() -> None:
    lab = Lab.of("a")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 150.0, "base": 100.0})
    (comparison,) = lab.comparisons()
    assert comparison.base == core.initial_state().run.facts.baseline
    assert comparison.candidate == revision("c1")
    assert (comparison.candidate_value, comparison.base_value) == (150.0, 100.0)
    assert comparison.metric == METRIC
    assert comparison.unit == "tok/s"
    assert comparison.direction == "max"


def test_a_faster_idea_is_better_a_slower_one_worse_and_one_in_the_noise_neither() -> None:
    results = {}
    for name, value in (("faster", 130.0), ("same", 100.5), ("slower", 70.0)):
        lab = Lab.of("a")
        lab.call("a", "c1", "input-1")
        lab.measure({"input-1": value, "base": 100.0})
        results[name] = lab.comparisons()[-1].verdict
    assert results == {
        "faster": core.ComparisonVerdict.BETTER,
        "same": core.ComparisonVerdict.WITHIN_NOISE,
        "slower": core.ComparisonVerdict.WORSE,
    }


def test_the_base_is_measured_once_however_many_candidates_are_compared_with_it() -> None:
    lab = Lab.of("a", "b")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 200.0, "base": 100.0})
    lab.call("a", "c2", "input-2")
    lab.measure({"input-2": 300.0, "base": 100.0})
    lab.call("b", "c3", "input-3")
    lab.measure({"input-3": 50.0, "base": 100.0})
    assert len(lab.baselines()) == 1
    assert [c.verdict for c in lab.comparisons()] == [
        core.ComparisonVerdict.BETTER,
        core.ComparisonVerdict.BETTER,
        core.ComparisonVerdict.WORSE,
    ]


def test_a_base_still_running_for_one_attempt_is_not_measured_again_for_another() -> None:
    lab = Lab.of("a", "b")
    lab.call("a", "c1", "input-1")
    asked = lab.call("b", "c2", "input-2")
    assert [r.plan.purpose for r in asked] == ["local-validation"]
    lab.measure({"input-1": 200.0, "input-2": 90.0, "base": 100.0})
    assert len(lab.baselines()) == 1
    assert {c.candidate for c in lab.comparisons()} == {revision("c1"), revision("c2")}


def test_a_base_older_than_the_staleness_bound_is_measured_again_for_the_next_candidate() -> None:
    lab = Lab.of("a")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 200.0, "base": 100.0})
    lab.advance(STALENESS)
    lab.call("a", "c2", "input-2")
    assert len(lab.baselines()) == 1, "a sample exactly as old as the bound is still fresh"
    lab.measure({"input-2": 300.0, "base": 100.0})
    lab.advance(1.0)
    lab.call("a", "c3", "input-3")
    assert len(lab.baselines()) == 2
    lab.measure({"input-3": 300.0, "base": 100.0})
    assert [c.base_samples for c in lab.comparisons()][-1] == 1, "only the fresh sample counts"


def test_a_close_call_earns_the_base_one_more_run_and_a_decisive_result_does_not() -> None:
    lab = Lab.of("a")
    lab.call("a", "close", "input-1")
    lab.measure({"input-1": 101.0, "base": 100.0})
    assert len(lab.baselines()) == 2, "within noise of the bar: the base is measured again"
    (first, second) = lab.comparisons()
    assert first.verdict is second.verdict is core.ComparisonVerdict.WITHIN_NOISE
    assert (first.base_samples, second.base_samples) == (1, 2)
    assert second.noise < first.noise

    decisive = Lab.of("a")
    decisive.call("a", "far", "input-1")
    decisive.measure({"input-1": 300.0, "base": 100.0})
    assert len(decisive.baselines()) == 1


def test_a_close_call_ended_by_the_strategys_base_run_is_sampled_again() -> None:
    """The base run a close call earns is core's own, whichever job's end earned it.

    The strategy measures the run's input (its ``Measure`` decision, in the run's scope) while
    an agent's candidate runs. The base's end makes the close call, so core allocated the
    extra base run while it stepped that decision's job, and stamped it with the decision.
    The decision measured another scope, so the run's origin no longer proved out and its
    reading was dropped: the comparison never got its second base sample.
    """
    lab = Lab.of("a")
    run_scope = core.Scope(owner=lab.state.run.run_id, generation=lab.state.run.generation)
    base_plan = plan(
        purpose="baseline",
        candidate=lab.state.run.facts.baseline,
        measured_input="base",
        dedicated_measurement_resources=True,
    )
    lab.state = transition(
        lab.state,
        core.DecisionSubmitted(
            decision=core.Wait(
                request=core.Measure(
                    decision_id=core.DecisionId(root="baseline"), scope=run_scope, plan=base_plan
                ),
                until=core.Within(limit=100.0),
            ),
            expected_revision=lab.state.revision,
        ),
    ).state
    (candidate,) = lab.call("a", "close", "input-1")
    (base,) = lab.baselines()
    for request, value in ((candidate, 100.3), (base, 100.0)):
        assert request.request_id is not None
        lab.ended.add(request.request_id.root)
        lab.state = ended_with(lab.state, request, value)
    (first,) = lab.comparisons()
    assert core.close_call(first)
    extra = [r for r in lab.baselines() if r.scope == candidate.scope]
    assert [r.decision_id for r in extra] == [None], "the extra base run is core's own"
    lab.measure({"input-1": 100.3, "base": 100.0})
    assert [c.base_samples for c in lab.comparisons()] == [1, 2]


def test_samples_of_one_input_aggregate_and_a_lucky_run_changes_nothing_but_the_mean() -> None:
    """An agent that re-runs one input (a notes-only commit each time) is averaged, not picked."""
    lab = Lab.of("a")
    values = {"input-1": 0.0, "base": 100.0}
    runs = (101.0, 99.0, 103.0, 200.0)  # the last is a lucky outlier
    for number, value in enumerate(runs):
        values["input-1"] = value
        lab.call("a", f"c{number}", "input-1")
        lab.measure(values)
    samples = [s for s in lab.state.evaluation.samples if s.measured_input == "input-1"]
    assert [s.reading.value for s in samples] == list(runs)
    last = lab.comparisons()[-1]
    assert last.candidate_samples == len(runs)
    assert math.isclose(last.candidate_value, sum(runs) / len(runs))
    assert last.candidate_value != max(runs)


def test_a_sample_is_recorded_only_for_a_run_that_names_its_measured_input() -> None:
    lab = Lab.of("a")
    state = lab.state
    lab.state = transition(
        state,
        core.AgentMeasurementRequested(
            scope=lab.scopes["a"],
            plan=measured("local-validation", "c1", None),
            within=core.Within(limit=100.0),
            call_id="c",
        ),
    ).state
    (request,) = submissions(lab.state)
    lab.state = ended_with(lab.state, request, 100.0)
    assert lab.state.evaluation.samples == ()


def test_a_sample_cannot_be_constructed_without_its_commit_or_measured_input() -> None:
    sample = core.Sample(
        run=core.RequestId(root="r"),
        scope=core.Scope(owner=core.AttemptId(root="a"), generation=0),
        commit=revision("c"),
        measured_input="sha256:x",
        evaluation="e",
        gates=("benchmark",),
        host="h",
        dedicated_resources=False,
        reading=reading(1.0),
        observed_at=0.0,
    )
    for missing, empty in (("commit", None), ("measured_input", None), ("measured_input", "")):
        with pytest.raises(ValueError, match=missing):
            core.Sample.model_validate(sample.model_dump() | {missing: empty})


def test_a_sample_records_its_gates_host_and_whether_the_host_was_held_alone() -> None:
    lab = Lab.of("a")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 110.0, "base": 100.0})
    sample = next(s for s in lab.state.evaluation.samples if s.measured_input == "input-1")
    assert sample.gates == ("accuracy", "benchmark")
    assert sample.host == "environment"
    assert sample.dedicated_resources is True
    assert sample.commit == revision("c1")
    assert sample.base == core.BaseRef(
        commit=core.initial_state().run.facts.baseline, measured_input="base"
    )


def test_the_comparison_a_resume_publishes_is_the_one_the_samples_make() -> None:
    lab = Lab.of("a")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 130.0, "base": 100.0})
    now = core.moment_of(lab.state.run)
    kept = core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["input-1"], now)
    assert kept == lab.comparisons()
    assert core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["other"], now) == ()


@pytest.mark.regression
@settings(max_examples=examples(pr=30, nightly=300))
@given(
    wait=st.floats(min_value=0.0, max_value=3 * STALENESS),
    again=st.booleans(),
)
@example(wait=STALENESS + 1.0, again=False)
@example(wait=STALENESS, again=False)
def test_a_comparison_whose_base_went_stale_is_not_presented_as_held_now(
    *, wait: float, again: bool
) -> None:
    """A resume receipt presents a comparison only while its base's samples still count.

    The log of comparisons only grows, so before, after ``base_staleness`` with no new sample
    the receipt (and the tool's reply) still told the agent the old verdict "as held now". The
    comparison presented is the one the samples held at that moment make, or none. With
    ``again`` a later call re-measures the base, and the fresh comparison is presented.
    """
    lab = Lab.of("a")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 130.0, "base": 100.0})
    lab.advance(wait)
    if again:
        lab.call("a", "c2", "input-1")
        lab.measure({"input-1": 130.0, "base": 100.0})
    now = core.moment_of(lab.state.run)
    presented = core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["input-1"], now)
    held = core.comparisons_of(lab.state.evaluation, now)
    assert presented == held
    if not again:
        assert (presented == ()) == (wait > STALENESS)


@pytest.mark.regression
def test_a_comparison_whose_base_went_partly_stale_is_recomputed_over_the_rest() -> None:
    """When only some base samples went stale, the comparison is the one the fresh ones make.

    The base reads 100 at the start; a close call on another input earns it a second run, which
    reads 104, three quarters of the staleness bound later. Half a bound after that the first
    base run no longer counts. The log's entry for the first input still rests on the base run
    of 100, but what is presented is the comparison with the base run of 104 alone, never
    dropped while a fresh base sample exists.
    """
    lab = Lab.of("a")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 130.0, "base": 100.0})
    lab.advance(0.75 * STALENESS)
    lab.call("a", "c2", "input-2")
    lab.measure({"input-2": 101.0, "base": 104.0})
    assert len(lab.baselines()) == 2, "the close call on input-2 earned the base a second run"
    lab.advance(0.5 * STALENESS)
    now = core.moment_of(lab.state.run)
    (presented,) = core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["input-1"], now)
    assert (presented.base_samples, presented.base_value) == (1, 104.0)
    assert presented.verdict is core.ComparisonVerdict.BETTER
    logged = [c for c in lab.comparisons() if c.candidate_input == "input-1"]
    assert presented not in logged, "the log holds only the comparison with the base run of 100"


@pytest.mark.regression
def test_an_input_presents_the_comparison_of_its_newest_evaluation() -> None:
    """An input measured in a second evaluation: one comparison, the newest evaluation's.

    The agent compares one input with its base, then evaluates it under another evaluation
    (another environment). The comparison presented is the newest evaluation's, not the older
    one's.
    """
    lab = Lab.of("a")
    values = {"input-1": 130.0, "base": 100.0}
    lab.call("a", "c1", "input-1")
    lab.measure(values)
    now = core.moment_of(lab.state.run)
    (first,) = core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["input-1"], now)
    assert (first.base_input, first.candidate_samples) == ("base", 1)
    lab.call("a", "c3", "input-1", variant="environment")
    lab.measure(values | {"input-1": 140.0})
    now = core.moment_of(lab.state.run)
    (latest,) = core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["input-1"], now)
    assert (latest.base_input, latest.candidate_samples, latest.candidate_value) == (
        "base",
        1,
        140.0,
    )


@pytest.mark.regression
def test_a_verdict_from_a_zero_noise_under_a_declared_floor_is_no_close_call() -> None:
    """A candidate that reads 0 under a declared floor is ``worse``, decisively.

    Its ratio is 0, so its noise is 0 too, but the task declared its noise: the verdict stands
    on it. Keying the close call on a zero noise instead (review H) took one more base run and
    made the official check run afresh for a candidate that measured nothing.
    """
    lab = Lab.of("a")
    first = lab.call("a", "c1", "input-1")[0]
    lab.measure({"input-1": 0.0, "base": 100.0})
    (comparison,) = lab.comparisons()
    assert (comparison.verdict, comparison.noise) == (core.ComparisonVerdict.WORSE, 0.0)
    assert not core.close_call(comparison)
    assert len(lab.baselines()) == 1, "no extra base run"
    assert final_check(lab, "a", "c1", "input-1").reuses == first.request_id


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0])
@pytest.mark.parametrize("cause", ["stale", "failed"])
def test_an_official_check_whose_base_has_no_fresh_sample_runs_afresh(
    floor: float, cause: str
) -> None:
    """With no fresh base sample there is no verdict, so the agent's run does not answer.

    The agent's one run is decisive against the base under a declared floor, and a close call
    without one. Then the base's samples go stale, or the base's only run failed (it gives no
    sample). Before, no comparison meant "nothing to ask for", and the official check reused
    the agent's single run, which then became the ranked answer on no comparison at all.
    """
    lab = Lab.of("a", floor=floor)
    asked = lab.call("a", "c1", "input-1")
    if cause == "stale":
        lab.measure({"input-1": 300.0, "base": 100.0})
        lab.advance(STALENESS + 1.0)
    else:
        for request in asked:
            assert request.request_id is not None
            lab.ended.add(request.request_id.root)
            failed = request.plan.purpose == "baseline"
            lab.state = ended_with(lab.state, request, 100.0 if failed else 300.0, failed=failed)
    now = core.moment_of(lab.state.run)
    assert core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["input-1"], now) == ()
    check = final_check(lab, "a", "c1", "input-1")
    assert check.reuses is None, "the official check runs afresh"


def final_check(
    lab: Lab, name: str, commit: str, candidate_input: str, *, compared: bool = True
) -> core.SubmitMeasurement:
    """The strategy's official measurement of an input in the attempt's scope: its submission.

    ``compared`` is whether the strategy reads the comparison with the attempt's base.
    """
    decision = core.Measure(
        decision_id=core.DecisionId(root=f"official-{commit}"),
        scope=lab.scopes[name],
        plan=measured("official", commit, candidate_input),
        compared=compared,
    )
    asked = transition(
        lab.state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=100.0)),
            expected_revision=lab.state.revision,
        ),
    )
    (submission,) = [
        r
        for r in asked.requests
        if isinstance(r, core.SubmitMeasurement) and r.plan.purpose == "official"
    ]
    lab.state = asked.state
    return submission


def test_the_final_check_of_a_close_call_adds_its_own_sample_to_the_same_measurement() -> None:
    """Another run of the input the agent measured: the same measurement, the same base."""
    lab = Lab.of("a")
    lab.call("a", "close", "input-1")
    lab.measure({"input-1": 101.0, "base": 100.0})
    check = final_check(lab, "a", "close", "input-1")
    assert check.reuses is None, "a close call is not settled by the samples held"
    assert check.request_id is not None
    lab.ended.add(check.request_id.root)
    lab.state = ended_with(lab.state, check, 102.0)
    sample = lab.state.evaluation.samples[-1]
    assert sample.run == check.request_id
    assert sample.base == core.BaseRef(
        commit=core.initial_state().run.facts.baseline, measured_input="base"
    )
    assert lab.comparisons()[-1].candidate_samples == 2


def test_the_final_check_of_a_decisive_result_is_answered_by_the_samples_held() -> None:
    lab = Lab.of("a")
    first = lab.call("a", "far", "input-1")[0]
    lab.measure({"input-1": 300.0, "base": 100.0})
    check = final_check(lab, "a", "far", "input-1")
    assert check.reuses == first.request_id


def official_reading(lab: Lab, check: core.SubmitMeasurement) -> core.Reading:
    """The headline reading the ledger's benchmark record of the official check states."""
    (record,) = (
        item
        for item in lab.state.evaluation.evidence
        if item.source_request == check.request_id and item.kind is core.EvidenceKind.BENCHMARK
    )
    assert record.reading is not None
    return record.reading


@pytest.mark.regression
@settings(max_examples=examples(pr=30, nightly=300))
@given(
    values=st.lists(st.floats(min_value=50.0, max_value=400.0), min_size=1, max_size=4),
    official=st.floats(min_value=50.0, max_value=400.0),
    reload=st.booleans(),
)
@example(values=[300.0, 250.0], official=280.0, reload=False)
@example(values=[101.0, 104.0], official=99.0, reload=True)
def test_the_official_record_states_the_value_its_comparison_compared(
    *, values: list[float], official: float, reload: bool
) -> None:
    """The ledger's official record, which ranking reads, states the comparison's candidate value.

    The agent re-runs one input any number of times, then the strategy asks for the official
    measurement: answered by those samples when they are decisive, or by one more run when they
    are a close call. Before, the record that carried the answer through the ledger read the
    first run's value while the comparison stated the mean, so the agent saw one number and the
    leaderboard ranked another. Each run's own reading stays in its sample.
    """
    lab = Lab.of("a")
    for number, value in enumerate(values):
        lab.call("a", f"c{number}", "input-1")
        lab.measure({"input-1": value, "base": 100.0})
    if reload:
        lab.state = reloaded(lab.state)
    check = final_check(lab, "a", f"c{len(values) - 1}", "input-1")
    assert check.request_id is not None
    lab.ended.add(check.request_id.root)
    if check.reuses is None:
        lab.state = ended_with(lab.state, check, official)
    else:
        source = next(s for s in lab.state.evaluation.samples if s.run == check.reuses)
        lab.state = ended_with(lab.state, check, source.reading.value, reused=check.reuses)
    stated = official_reading(lab, check)
    assert stated.value == lab.comparisons()[-1].candidate_value
    (ranked,) = (
        item
        for item in lab.state.evaluation.measurements_of(revision(f"c{len(values) - 1}"))
        if item.purpose == "official" and item.kind is core.EvidenceKind.BENCHMARK
    )
    assert ranked.reading == stated
    runs = [s.reading.value for s in lab.state.evaluation.samples if s.measured_input == "input-1"]
    assert runs == [*values, *([official] if check.reuses is None else [])]
    assert reloaded(lab.state) == lab.state


@pytest.mark.regression
def test_a_task_that_declares_no_noise_gets_no_verdict_from_runs_that_show_none() -> None:
    """With no declared ``relative_noise``, identical runs measure no noise either.

    Before, the close call earned the base one more run, and when it read what the first did
    (a quantized or cached benchmark), the spread was zero and the 0.3% gap between the two
    sides was ``better``: the official check then reused the agent's single run. Now each side
    takes three runs (the fewest that can give a verdict) and no more, the comparison stays
    ``inconclusive``, and since no run can change that (`verdict_unreachable`) the official
    check is answered by the samples held instead of a run that would read the same.
    """
    lab = Lab.of("a", floor=0.0)
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 100.3, "base": 100.0})
    last = lab.comparisons()[-1]
    assert (last.candidate_samples, last.base_samples) == (3, 3)
    assert {c.verdict for c in lab.comparisons()} == {core.ComparisonVerdict.INCONCLUSIVE}
    assert core.verdict_unreachable(last, core.moment_of(lab.state.run).policy)
    assert len(lab.baselines()) == core.MIN_SAMPLES_WITHOUT_FLOOR, "at most two extra base runs"
    check = final_check(lab, "a", "c1", "input-1")
    assert check.reuses is not None, "no run can settle it: the samples held answer"


def test_three_runs_a_side_without_a_declared_floor_give_a_verdict() -> None:
    """One evaluation of one commit: core runs it and its base three times each, with a spread."""
    lab = Lab.of("a", floor=0.0)
    lab.call("a", "c1", "input-1")
    readings = {"input-1": iter((110.0, 111.0, 109.0)), "base": iter((100.0, 101.0, 99.0))}
    while running := [
        r
        for r in submissions(lab.state)
        if r.request_id is not None and r.request_id.root not in lab.ended
    ]:
        request = running[0]
        assert request.request_id is not None
        assert request.plan.measured_input is not None
        lab.ended.add(request.request_id.root)
        lab.state = ended_with(lab.state, request, next(readings[request.plan.measured_input]))
    last = lab.comparisons()[-1]
    assert (last.candidate_samples, last.base_samples) == (3, 3)
    assert last.verdict is core.ComparisonVerdict.BETTER
    assert not core.close_call(last)
    assert final_check(lab, "a", "c1", "input-1").reuses is not None


@pytest.mark.regression
@settings(max_examples=examples(pr=20, nightly=200))
@given(official=st.floats(min_value=1.0, max_value=400.0), reused=st.booleans())
@example(official=80.0, reused=False)
@example(official=80.0, reused=True)
def test_a_failed_official_record_keeps_the_reading_its_run_reported(
    *, official: float, reused: bool
) -> None:
    """The official answer states the samples' mean only for a record that passed.

    A benchmark can report its metrics and then fail a check, so its FAILED record carries a
    reading. Before, the official record was restated as the mean of the runs that passed, so
    a failed run read as a passing number. Without ``reused`` the official check runs afresh
    and fails; with it the agent's own run of the input failed, and the official measurement
    re-reads that run, as the reuse rule prefers a failed run.
    """
    lab = Lab.of("a")
    lab.call("a", "c1", "input-1")
    lab.measure({"input-1": 200.0 if reused else 101.0, "base": 100.0})
    if reused:
        asked = lab.call("a", "c2", "input-1")
        (failing,) = (r for r in asked if r.plan.purpose == "local-validation")
        assert failing.request_id is not None
        lab.ended.add(failing.request_id.root)
        lab.state = ended_with(lab.state, failing, official, failed=True)
        lab.measure({"base": 100.0})
        check = final_check(lab, "a", "c2", "input-1")
        assert check.reuses == failing.request_id
    else:
        check = final_check(lab, "a", "c1", "input-1")
        assert check.reuses is None, "a close call runs afresh"
    assert check.request_id is not None
    lab.ended.add(check.request_id.root)
    lab.state = ended_with(lab.state, check, official, reused=check.reuses, failed=True)
    stated = official_reading(lab, check)
    assert stated.value == official


def test_an_official_run_is_reused_by_its_own_reading_not_the_answer_its_record_states() -> None:
    """A second official measurement of the input re-reads the first official run as it ran.

    The first official run of the input is the first conclusive run, so it carries the answer
    for a later official measurement of the same input (another commit of the same tree). Its
    ledger record states the mean of the samples, but the runtime re-reports the run's own
    reading, so the reused record is checked against that (``run_reading``); checked against
    the stated mean it would be refused and the measurement would run afresh.
    """
    lab = Lab.of("a")
    first = final_check(lab, "a", "c1", "input-1")
    assert first.reuses is None, "no run of the input yet"
    lab.state = started_with(lab.state, first)
    (base,) = lab.baselines()
    (agent,) = lab.call("a", "c2", "input-1")
    for request, value in ((agent, 200.0), (base, 100.0)):
        assert request.request_id is not None
        lab.ended.add(request.request_id.root)
        lab.state = ended_with(lab.state, request, value)
    assert first.request_id is not None
    lab.ended.add(first.request_id.root)
    lab.state = finished_with(lab.state, first, 210.0)
    assert official_reading(lab, first).value == 205.0, "the mean of 200 and 210"
    second = final_check(lab, "a", "c3", "input-1")
    assert second.reuses == first.request_id
    assert second.request_id is not None
    lab.ended.add(second.request_id.root)
    lab.state = ended_with(lab.state, second, 210.0, reused=first.request_id)
    assert official_reading(lab, second).value == 205.0
    (job,) = (j for j in lab.state.evaluation.jobs if j.submission_id == second.request_id)
    assert job.status is core.ObservationStatus.SUCCEEDED
    assert len(job.evidence) == 2, "every record of the re-read run was accepted"


def fresh_base_samples(lab: Lab) -> list[core.Sample]:
    return [
        s
        for s in lab.state.evaluation.samples
        if s.measured_input == "base" and lab.state.run.now_at - s.observed_at <= STALENESS
    ]


def base_budget_spent(lab: Lab, name: str) -> bool:
    """Whether the attempt's submission budget for the base's identity has no run left."""
    return any(
        b.identity.purpose == "baseline"
        and b.scope == lab.scopes[name]
        and len(b.receipts) >= min(b.limit, core.MAX_SUBMISSIONS_PER_MEASUREMENT)
        for b in lab.state.evaluation.submission_budgets
    )


def base_running(lab: Lab) -> bool:
    return any(
        r.plan.purpose == "baseline"
        and r.request_id is not None
        and r.request_id.root not in lab.ended
        for r in submissions(lab.state)
    )


@settings(max_examples=examples(pr=40, nightly=400))
@given(
    calls=st.lists(
        st.tuples(
            st.sampled_from(("a", "b")),
            st.integers(min_value=0, max_value=3),
            st.sampled_from((0.3, 0.7, 1.0, 1.01, 1.5, 3.0)),
            st.sampled_from((0.0, 1.0, STALENESS + 1.0)),
        ),
        min_size=1,
        max_size=7,
    ),
    reload_at=st.integers(min_value=0, max_value=7),
    floor=st.sampled_from((FLOOR, 0.0)),
)
@example(calls=[("a", 0, 1.01, 0.0)], reload_at=7, floor=FLOOR)
@example(calls=[("a", 0, 1.01, 0.0), ("a", 0, 1.0, 1.0)], reload_at=1, floor=0.0)
def test_the_base_is_measured_once_per_input_and_again_only_on_staleness_or_a_close_call(
    calls: list[tuple[str, int, float, float]], reload_at: int, floor: float
) -> None:
    """Over any sequence of evaluations by two attempts with the clock moving between them.

    A call allocates a run of the base exactly when no fresh sample of it is held and none is
    running. The only other runs of the base are the re-measures a close call earns once a
    candidate's samples end, so no base is ever measured twice for a decisive result, and a
    close call never takes the base past ``BASE_MAX_SAMPLES`` fresh samples under a declared
    floor (one extra run) or ``MIN_SAMPLES_WITHOUT_FLOOR`` without one (two extra runs). A
    reload loses nothing.
    """
    lab = Lab.of("a", "b", floor=floor)
    most = core.BASE_MAX_SAMPLES if floor else core.MIN_SAMPLES_WITHOUT_FLOOR
    values: dict[str, float] = {"base": 100.0}
    for number, (name, input_number, factor, wait) in enumerate(calls):
        if number == reload_at:
            lab.state = reloaded(lab.state)
        lab.advance(wait)
        key = f"input-{input_number}"
        values[key] = 100.0 * factor
        held, running, before = fresh_base_samples(lab), base_running(lab), len(lab.baselines())
        spent = base_budget_spent(lab, name)
        lab.call(name, f"c{number}", key)
        allocated = len(lab.baselines()) - before
        assert allocated == (0 if held or running or spent else 1), (held, running, spent)
        logged = len(lab.comparisons())
        after_call = len(lab.baselines())
        lab.measure(values)
        remeasures = len(lab.baselines()) - after_call
        if remeasures:
            assert any(core.close_call(c) for c in lab.comparisons()[logged:]), (
                "a base run beyond the first is earned by a close call"
            )
        assert len(fresh_base_samples(lab)) <= most
    assert reloaded(lab.state) == lab.state


@settings(max_examples=examples(pr=30, nightly=300))
@given(
    values=st.lists(st.floats(min_value=50.0, max_value=200.0), min_size=1, max_size=6),
    base=st.floats(min_value=50.0, max_value=200.0),
    seed=st.randoms(use_true_random=False),
)
def test_a_comparison_is_the_aggregate_of_every_sample_in_any_order(
    values: list[float], base: float, seed: random.Random
) -> None:
    """The ratio is the mean of all the candidate's samples over the base's, whatever the order.

    Samples are taken as an agent re-running one input would (a commit each time, one measured
    input), and the comparison is then recomputed from them in a shuffled order: nothing in it
    depends on which run came first or last, so no run can be picked or favoured.
    """
    lab = Lab.of("a")
    for number, value in enumerate(values):
        lab.call("a", f"c{number}", "input-1")
        lab.measure({"input-1": value, "base": base})
    held = lab.comparisons()[-1]
    assert held.candidate_samples == len(values)
    assert math.isclose(held.candidate_value, sum(values) / len(values))
    candidate = [s for s in lab.state.evaluation.samples if s.measured_input == "input-1"]
    anchor = [s for s in lab.state.evaluation.samples if s.measured_input == "base"]
    seed.shuffle(candidate)
    seed.shuffle(anchor)
    named = next(s.base for s in candidate if s.base is not None)
    again = core.compare(
        tuple(candidate),
        tuple(anchor),
        scope=lab.scopes["a"],
        anchor=named,
        policy=core.policy_of(lab.state.run.facts),
    )
    assert again is not None
    assert math.isclose(again.ratio, held.ratio)
    assert math.isclose(again.noise, held.noise)
    assert again.verdict is held.verdict


def finished_for_search(
    state: core.CoreState, done: core.Transition
) -> list[core.MeasurementFinished]:
    """What the search that asked sees of a transition: the measurements that finished."""
    del state
    return [
        event
        for event in core.search_events(done.state, done.events)
        if isinstance(event, core.MeasurementFinished)
    ]


def test_a_finished_measurement_reaches_the_search_with_the_comparison_of_its_candidate() -> None:
    """The search that asked is told what the candidate compares to, not only its evidence.

    The agent's own run and the strategy's final check are samples of one input, so the
    comparison the check's result carries is over both."""
    lab = Lab.of("a")
    lab.call("a", "close", "input-1")
    lab.measure({"input-1": 101.0, "base": 100.0})
    check = final_check(lab, "a", "close", "input-1")
    assert check.request_id is not None
    lab.ended.add(check.request_id.root)
    done = ended_transition(lab.state, check, 102.0)
    (finished,) = finished_for_search(lab.state, done)
    assert finished.comparison is not None
    assert finished.comparison.candidate_samples == 2
    assert finished.comparison.ratio == pytest.approx(1.015)
    assert finished.comparison.base == core.initial_state().run.facts.baseline
    lab.state = done.state
    assert finished.comparison == lab.comparisons()[-1]


@pytest.mark.regression
def test_an_official_check_no_agent_evaluated_is_compared_with_the_attempts_base() -> None:
    """The strategy's own check of an idea names the attempt's base, as an agent's call does.

    Live, most ideas were measured only by the framework's final check, which named no base:
    they ended with no comparison, so no verdict and no noise. The check asks for the base's
    run beside its own, and its sample is compared once both land.
    """
    lab = Lab.of("a")
    check = final_check(lab, "a", "unseen", "input-9")
    (base,) = lab.baselines()
    assert check.base == core.BaseRef(
        commit=core.initial_state().run.facts.baseline, measured_input="base"
    )
    assert (base.plan.candidate, base.plan.measured_input) == (check.base.commit, "base")
    lab.measure({"input-9": 130.0, "base": 100.0})
    (held,) = lab.comparisons()
    assert (held.candidate_input, held.base_input, held.verdict) == (
        "input-9",
        "base",
        core.ComparisonVerdict.BETTER,
    )


def test_an_official_check_nobody_reads_a_comparison_of_costs_no_base_run() -> None:
    """A strategy that does not ask for the comparison is measured as before: no base run."""
    lab = Lab.of("a")
    check = final_check(lab, "a", "unseen", "input-9", compared=False)
    assert check.base is None
    assert lab.baselines() == []


def test_an_official_check_is_not_compared_when_the_runtime_gave_no_base_input() -> None:
    lab = Lab.of("a", base_input=None)
    check = final_check(lab, "a", "unseen", "input-9")
    assert check.base is None
    assert lab.baselines() == []


def test_a_measurement_of_an_input_no_agent_evaluated_reaches_the_search_without_one() -> None:
    lab = Lab.of("a")
    check = final_check(lab, "a", "unseen", "input-9")
    assert check.request_id is not None
    done = ended_transition(lab.state, check, 130.0)
    (finished,) = finished_for_search(lab.state, done)
    assert finished.comparison is None
