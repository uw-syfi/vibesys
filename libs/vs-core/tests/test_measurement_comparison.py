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
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_measurement_reuse import (
    measured,
    reloaded,
    revision,
    seen,
    submissions,
)
from .test_measurements import committed, transition

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
    name: str, base: core.RevisionRef, state: core.CoreState
) -> tuple[core.CoreState, core.Scope]:
    """``state`` with one more active attempt whose workspace was opened on ``base``."""
    owner = core.AttemptView(
        attempt_id=core.AttemptId(root=name),
        item_id=core.ItemId(root=f"item-{name}"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=base),
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


def ended_with(
    state: core.CoreState, request: core.SubmitMeasurement, value: float | None
) -> core.CoreState:
    """Accept ``request``'s job and end it passed, its benchmark record reading ``value``.

    With None the record carries no reading, as a benchmark that reported no metric does."""
    return ended_transition(state, request, value).state


def ended_transition(
    state: core.CoreState, request: core.SubmitMeasurement, value: float | None
) -> core.Transition:
    """`ended_with`, with the transition that ended the job: its events and its state."""
    started = seen(request, 1)
    state = committed(state, started)
    state = transition(state, core.MeasurementSubmissionObserved(observation=started)).state
    resource = started.resource_id
    assert resource is not None
    state = transition(state, core.JobObserved(resource_id=resource, observation=started)).state
    finished = seen(
        request,
        2,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        children_complete=True,
        observed_at=state.run.now_at,
    )
    assert isinstance(request.plan.candidate, core.RevisionRef)
    assert request.request_id is not None
    records = tuple(
        core.EvidenceRef(
            evidence_id=core.EvidenceId(root=f"{request.request_id.root}-{stage}"),
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
            status=core.ObservationStatus.SUCCEEDED,
            reading=reading(value) if stage == "benchmark" and value is not None else None,
        )
        for stage in ("accuracy", "benchmark")
    )
    state = committed(state, finished, _PASSED)
    state = transition(state, core.MeasurementSubmissionObserved(observation=finished)).state
    return transition(
        state,
        core.JobObserved(
            resource_id=resource,
            observation=finished,
            evidence=records,
            evaluation_result=_PASSED,
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
    def of(cls, *attempts: str, floor: float = FLOOR, staleness: float = STALENESS) -> Lab:
        state = stamped(core.initial_state(), floor=floor, staleness=staleness)
        scopes: dict[str, core.Scope] = {}
        for name in attempts:
            state, scopes[name] = attempt(name, state.run.facts.baseline, state)
        return cls(state=state, scopes=scopes)

    def call(
        self, name: str, commit: str, candidate_input: str, *, base_input: str = "base"
    ) -> list[core.SubmitMeasurement]:
        """The agent of attempt ``name`` evaluates ``commit``; the submissions core allocated."""
        before = len(submissions(self.state))
        self.calls += 1
        self.state = transition(
            self.state,
            core.AgentMeasurementRequested(
                scope=self.scopes[name],
                plan=measured("local-validation", commit, candidate_input),
                within=core.Within(limit=100.0),
                call_id=f"call-{self.calls}",
                base_input=base_input,
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
    lab = Lab.of("a")
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
            base_input="base",
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
    kept = core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["input-1"])
    assert kept == lab.comparisons()
    assert core.latest_comparisons(lab.state.evaluation, lab.scopes["a"], ["other"]) == ()


def final_check(lab: Lab, name: str, commit: str, candidate_input: str) -> core.SubmitMeasurement:
    """The strategy's official measurement of an input in the attempt's scope: its submission."""
    decision = core.Measure(
        decision_id=core.DecisionId(root=f"official-{commit}"),
        scope=lab.scopes[name],
        plan=measured("official", commit, candidate_input),
    )
    asked = transition(
        lab.state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=100.0)),
            expected_revision=lab.state.revision,
        ),
    )
    (submission,) = [r for r in asked.requests if isinstance(r, core.SubmitMeasurement)]
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
)
def test_the_base_is_measured_once_per_input_and_again_only_on_staleness_or_a_close_call(
    calls: list[tuple[str, int, float, float]], reload_at: int
) -> None:
    """Over any sequence of evaluations by two attempts with the clock moving between them.

    A call allocates a run of the base exactly when no fresh sample of it is held and none is
    running. The only other runs of the base are the re-measures a close call earns once a
    candidate's samples end, so no base is ever measured twice for a decisive result, and a
    close call never takes the base past ``BASE_MAX_SAMPLES`` fresh samples. A reload loses
    nothing.
    """
    lab = Lab.of("a", "b")
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
        assert len(fresh_base_samples(lab)) <= core.BASE_MAX_SAMPLES
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


def test_a_measurement_of_an_input_no_agent_evaluated_reaches_the_search_without_one() -> None:
    lab = Lab.of("a")
    check = final_check(lab, "a", "unseen", "input-9")
    assert check.request_id is not None
    done = ended_transition(lab.state, check, 130.0)
    (finished,) = finished_for_search(lab.state, done)
    assert finished.comparison is None
