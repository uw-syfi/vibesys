"""Unit and property tests for the ``HypothesisSearch`` facade.

Everything here drives ``HypothesisSearch`` through its public methods
(``initial``, ``next_round``, ``start``, ``attempts``, ``review_due``,
``official_due``, ``close_round``, ``frontier``, ``best``,
``pareto_conflict``) and inspects the resulting ``HypothesisState`` /
``Hypothesis`` values, rather than calling ``vibesys.hypothesis.
transitions`` directly: ``start`` and ``close_round`` already exercise the
deep per-round evidence projection (baseline selection, resolution,
retention) that used to be tested by calling ``transitions.start_hypothesis``/
``append_round`` by hand.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.declared_reply import DeclaredImplementerResponse

from vibesys.hypothesis import (
    CandidateDisposition,
    CarryOver,
    ClosedRound,
    Continue,
    Finished,
    HypothesisConfig,
    HypothesisOutcome,
    HypothesisResolution,
    HypothesisSearch,
    HypothesisStrategyUpdate,
    NewHypothesis,
    OrchestratorPlan,
    RoundRecord,
)
from vibesys.hypothesis import cadence as hypothesis_cadence
from vibesys.hypothesis.attempts import (
    AttemptState,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
)
from vibesys.hypothesis.state import Hypothesis, HypothesisState
from vibesys.metrics import MetricComparison, MetricSpace, Objective
from vs_sim.api.testing import examples

# --- helpers -----------------------------------------------------------


def _plan(
    identifier: str,
    *,
    base_round: int | None = None,
    updates: list[HypothesisStrategyUpdate] | None = None,
) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        hypothesis_updates=updates or [],
        task=f"implement {identifier}",
        pass_criteria="tests pass",  # noqa: S106  # LW-040073 [S106]; the argument is a fixture literal, not a credential.
        reasoning="test the claim",
        base_round=base_round,
    )


_UNSET_COMMIT: str | None = "__unset__"


def _round(  # noqa: PLR0913  # LW-040074 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    number: int,
    metric: float | None = None,
    *,
    hypothesis_id: str,
    commit: str | None = _UNSET_COMMIT,
    outcome: str | None = "proven",
    declared: str | None = "nominated",
    parent_round: int | None = None,
    parent_commit: str | None = None,
    direction: Literal["max", "min"] | None = "max",
    retained: bool | None = True,
    comparison: MetricComparison | None = None,
    passed: bool = True,
    reviewed: bool = True,
    judge_verdict: Literal["pass", "fail", "deferred"] | None = "pass",
    provenance: Literal["framework", "implementer"] | None = "framework",
    unit: str = "total_ops_per_sec",
    baseline_metric: float | None = None,
    delta_pct: float | None = None,
) -> RoundRecord:
    return RoundRecord(
        round_number=number,
        commit=f"{number:040x}" if commit is _UNSET_COMMIT else commit,
        perf_metric=metric,
        perf_unit=unit if metric is not None else None,
        passed=passed,
        reviewed=reviewed,
        hypothesis_id=hypothesis_id,
        hypothesis_declared_outcome=declared,
        judge_verdict=judge_verdict,
        hypothesis_outcome=outcome,
        hypothesis_claim=f"claim {hypothesis_id}",
        hypothesis_task=f"implement {hypothesis_id}",
        hypothesis_parent_round=parent_round,
        hypothesis_parent_commit=parent_commit,
        metrics={unit: metric} if metric is not None else {},
        official_evaluation=metric is not None,
        perf_direction=direction if metric is not None else None,
        perf_baseline_round=parent_round if baseline_metric is not None else None,
        perf_baseline_commit=parent_commit if baseline_metric is not None else None,
        perf_baseline_metric=baseline_metric,
        perf_delta_pct=delta_pct,
        candidate_retained=retained,
        perf_comparison=(
            comparison
            if comparison is not None or provenance != "framework" or metric is None
            else MetricComparison.INCOMPARABLE
        ),
        perf_provenance=provenance if metric is not None else None,
    )


def _closing_kwargs(*, passed: bool, reviewed: bool = True) -> dict:
    """Default ``close_round`` kwargs that finish (do not continue) a hypothesis."""
    return {
        "passed": passed,
        "reviewed": reviewed,
        "feedback": None,
        "keeps_active": False,
        "requests_continuation": False,
        "next_step": None,
        "terminal_needs_parent_choice": False,
    }


def _run_round(  # noqa: PLR0913  # LW-040075 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    search: HypothesisSearch,
    state: HypothesisState,
    records: list[RoundRecord],
    *,
    hypothesis_id: str,
    round_number: int,
    record: RoundRecord,
    passed: bool = True,
    reviewed: bool = True,
) -> HypothesisState:
    """Start ``hypothesis_id`` and close it in one round; return the new state.

    Each hypothesis here is a direct child of the round immediately before
    it (the base the search would resolve when that round was the best).
    """
    previous = next((r for r in records if r.round_number == round_number - 1), None)
    started = search.start(
        state,
        _plan(hypothesis_id),
        round_number=round_number,
        parent_round=None if previous is None else previous.round_number,
        parent_commit=None if previous is None else previous.commit,
    )
    closed = search.close_round(
        started.state,
        hypothesis=started.hypothesis,
        record=record,
        records=records,
        **_closing_kwargs(passed=passed, reviewed=reviewed),
    )
    return closed.state


# --- initial / next_round -----------------------------------------------


def test_initial_state_has_no_active_hypothesis() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    assert search.initial() == HypothesisState()


def test_next_round_is_finished_past_the_round_budget() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=3))
    decision = search.next_round(search.initial(), round_number=4, records=[], carry=CarryOver())
    assert isinstance(decision, Finished)


def test_next_round_continues_an_active_hypothesis() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    started = search.start(
        search.initial(), _plan("H-1"), round_number=1, parent_round=None, parent_commit=None
    )
    decision = search.next_round(started.state, round_number=1, records=[], carry=CarryOver())
    assert isinstance(decision, Continue)
    assert decision.hypothesis.hypothesis_id == "H-1"


def test_next_round_asks_for_a_new_hypothesis_when_none_is_active() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    decision = search.next_round(search.initial(), round_number=1, records=[], carry=CarryOver())
    assert isinstance(decision, NewHypothesis)


# --- start: basic lifecycle, rollback, strategy updates, validation ----


def test_start_activates_the_named_hypothesis() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    started = search.start(
        search.initial(), _plan("H-1"), round_number=1, parent_round=None, parent_commit=None
    )
    assert started.state.active_hypothesis_id == "H-1"
    assert started.hypothesis.hypothesis_id == "H-1"
    assert started.hypothesis.rounds == []


def test_start_rejects_a_blank_or_duplicate_hypothesis_id() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    state = search.initial()

    with pytest.raises(ValueError, match="blank"):
        search.start(state, _plan("  "), round_number=1, parent_round=None, parent_commit=None)

    started = search.start(
        state, _plan("H-1"), round_number=1, parent_round=None, parent_commit=None
    )
    closed = search.close_round(
        started.state,
        hypothesis=started.hypothesis,
        record=_round(1, hypothesis_id="H-1"),
        records=[],
        **_closing_kwargs(passed=True),
    )
    with pytest.raises(ValueError, match="already exists"):
        search.start(
            closed.state, _plan("H-1"), round_number=2, parent_round=None, parent_commit=None
        )


def test_start_rejects_starting_while_another_hypothesis_is_active() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    started = search.start(
        search.initial(), _plan("H-1"), round_number=1, parent_round=None, parent_commit=None
    )
    with pytest.raises(ValueError, match="another"):
        search.start(
            started.state, _plan("H-2"), round_number=2, parent_round=None, parent_commit=None
        )


def test_start_applies_strategy_updates_to_completed_hypotheses() -> None:
    """A designer's parked/abandoned update lands on the named hypothesis."""
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    started = search.start(
        search.initial(), _plan("old"), round_number=1, parent_round=None, parent_commit=None
    )
    closed = search.close_round(
        started.state,
        hypothesis=started.hypothesis,
        record=_round(1, 100.0, hypothesis_id="old"),
        records=[],
        **_closing_kwargs(passed=True),
    )
    update = HypothesisStrategyUpdate(
        hypothesis_id="old", disposition="abandoned", reason="A better direction supersedes it."
    )
    started_new = search.start(
        closed.state,
        _plan("new", updates=[update]),
        round_number=2,
        parent_round=None,
        parent_commit=None,
    )
    old = started_new.state.by_id("old")
    assert old is not None
    assert old.strategy.value == "abandoned"
    assert started_new.state.active_hypothesis_id == "new"


def test_start_strategy_update_rejects_an_unknown_hypothesis() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    update = HypothesisStrategyUpdate(
        hypothesis_id="missing", disposition="parked", reason="no evidence"
    )
    with pytest.raises(ValueError, match="unknown hypothesis"):
        search.start(
            search.initial(),
            _plan("H-1", updates=[update]),
            round_number=1,
            parent_round=None,
            parent_commit=None,
        )


def test_start_records_the_base_the_caller_resolved() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=10))
    started = search.start(
        HypothesisState(),
        _plan("H-3", base_round=1),
        round_number=3,
        parent_round=1,
        parent_commit="a" * 40,
    )
    assert started.hypothesis.parent_round == 1
    assert started.hypothesis.parent_commit == "a" * 40
    assert started.hypothesis.plan.base_round == 1


def test_start_on_the_input_records_no_parent_round() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=10))
    started = search.start(
        HypothesisState(),
        _plan("H-3", base_round=0),
        round_number=3,
        parent_round=None,
        parent_commit="0" * 40,
    )
    assert started.hypothesis.parent_round is None
    assert started.hypothesis.parent_commit == "0" * 40


def test_plan_rejects_a_negative_base_round() -> None:
    with pytest.raises(ValidationError, match="base_round"):
        _plan("H-1", base_round=-1)


# --- attempts: never exceed budget --------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    retry=st.integers(min_value=1, max_value=20), max_retries=st.integers(min_value=1, max_value=10)
)
def test_attempts_never_exceed_budget(retry: int, max_retries: int) -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5, max_retries_per_round=max_retries))
    budget = search.attempts(retry=retry)
    assert budget.max_retries == max_retries
    assert budget.remaining >= 0
    assert budget.exhausted == (retry > max_retries)


# --- review_due / official_due cadence -----------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    round_number=st.integers(min_value=1, max_value=20),
    max_rounds=st.integers(min_value=1, max_value=20),
    judge_every=st.integers(min_value=1, max_value=5),
    outcome=st.sampled_from(list(HypothesisOutcome)),
    candidate_evidence_fresh=st.booleans(),
)
def test_review_due_at_cadence_final_round_or_fresh_claim(
    round_number: int,
    max_rounds: int,
    judge_every: int,
    outcome: HypothesisOutcome,
    candidate_evidence_fresh: bool,  # noqa: FBT001  # LW-040076 [FBT001]; the boolean is a flag in a parametrized test case, not a public call signature.
) -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=max_rounds, judge_every=judge_every))
    due = search.review_due(
        round_number=round_number,
        outcome=outcome,
        candidate_evidence_is_fresh=candidate_evidence_fresh,
    )
    expected = (
        round_number == max_rounds
        or round_number % judge_every == 0
        or outcome in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
        or candidate_evidence_fresh
    )
    assert due == expected


def test_review_due_override_forces_an_already_started_review_to_finish() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=10, judge_every=5))
    assert search.review_due(
        round_number=2,
        outcome=HypothesisOutcome.CONTINUE,
        review_started=True,
        requests_continuation=False,
    )


def test_review_due_override_skips_a_bounded_already_reviewed_continuation() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=10, judge_every=5))
    due = search.review_due(
        round_number=2,
        outcome=HypothesisOutcome.CONTINUE,
        review_started=True,
        requests_continuation=True,
        pareto_frontier_claim=False,
        revalidation_required=False,
    )
    assert not due


@given(candidate_ready=st.booleans())
def test_official_due_measures_every_ready_candidate(*, candidate_ready: bool) -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=10))
    reason = search.official_due(candidate_ready=candidate_ready)
    if candidate_ready:
        assert reason == hypothesis_cadence.MEASURED_AFTER_REVIEW
    else:
        assert reason is None


# --- close_round: determinism and lease bounds --------------------------


def _attempt_state(
    *,
    passed: bool,
    outcome: HypothesisOutcome,
    next_step: str = "keep going",
    feedback: str | None = None,
) -> AttemptState:
    implementation = DeclaredImplementerResponse(
        declared=outcome,
        next_step=next_step,
        summary="did the thing",
        expected_behavior="it works",
    )
    return AttemptState(
        agent_run_state=HypothesisState(),
        feedback=feedback,
        implementation=implementation,
        judge=JudgeReviewed("pass") if passed else JudgeSkipped(JudgeSkipReason.NOT_REACHED),
        passed=passed,
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    passed=st.booleans(),
    reviewed=st.booleans(),
    outcome=st.sampled_from(
        [HypothesisOutcome.CONTINUE, HypothesisOutcome.NOMINATED, HypothesisOutcome.DISPROVEN]
    ),
    continuation_rounds=st.integers(min_value=0, max_value=3),
)
def test_close_round_is_deterministic_and_bounds_the_lease(
    *, passed: bool, reviewed: bool, outcome: HypothesisOutcome, continuation_rounds: int
) -> None:
    """``close_round`` never grows a hypothesis's rounds past one per call,
    never retains an active hypothesis once it has passed, and produces the
    same output for the same input (it is a pure function of its arguments).
    """
    plan = _plan("H-1")
    search = HypothesisSearch(HypothesisConfig(max_rounds=5, max_retries_per_round=3))
    started = search.start(
        search.initial(), plan, round_number=1, parent_round=None, parent_commit=None
    )
    state = started.state
    hypothesis = started.hypothesis
    assert hypothesis is not None
    hypothesis.continuation_rounds = continuation_rounds

    next_step = "keep going" if outcome is HypothesisOutcome.CONTINUE else ""
    attempt = _attempt_state(
        passed=passed, outcome=outcome, next_step=next_step, feedback="feedback text"
    )
    record = _round(1, hypothesis_id="H-1", commit="a" * 40, outcome=outcome.value)
    record = replace(
        record,
        passed=passed,
        judge_verdict=("pass" if passed else "fail") if reviewed else "deferred",
    )

    keeps_active = hypothesis_cadence.keeps_hypothesis_active(
        outcome=outcome,
        next_step=next_step,
        continuation_rounds=continuation_rounds,
        max_continuation_rounds=search.config.max_continuation_rounds,
    )
    requests_continuation = bool(
        outcome
        in {
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.IMPLEMENTATION_FAILED,
            HypothesisOutcome.INCONCLUSIVE,
        }
        and next_step.strip()
    )
    terminal_needs_parent_choice = (
        attempt.implementation is not None
        and not keeps_active
        and outcome is not HypothesisOutcome.NOMINATED
    )

    def close() -> ClosedRound:
        return search.close_round(
            state,
            hypothesis=hypothesis,
            record=record,
            records=[],
            passed=passed,
            reviewed=reviewed,
            feedback=attempt.feedback,
            keeps_active=keeps_active,
            requests_continuation=requests_continuation,
            next_step=next_step or None,
            terminal_needs_parent_choice=terminal_needs_parent_choice,
        )

    first = close()
    second = close()
    assert first.state.model_dump() == second.state.model_dump()
    assert len(first.state.rounds) == 1
    if passed and not keeps_active:
        assert first.state.active_hypothesis_id is None


# --- close_round: round-evidence projection regressions ------------------


def test_within_noise_delta_resolves_inconclusive() -> None:
    """Regression for #507: a 1% delta under a 5% tolerance is not a result.

    ``close_round`` is the loop's own writer, so a delta it cannot resolve
    to a comparison must not silently record the opposite verdict from the
    one the round observed.
    """
    space = MetricSpace(
        objectives=(Objective(name="total_ops_per_sec", direction="max"),), relative_noise=0.05
    )
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    state = HypothesisState(metrics=space)
    state = _run_round(
        search,
        state,
        [],
        hypothesis_id="H-base",
        round_number=1,
        record=_round(1, 100.0, hypothesis_id="H-base"),
    )
    records = state.rounds
    state = _run_round(
        search,
        state,
        records,
        hypothesis_id="H-1",
        round_number=2,
        record=_round(
            2,
            101.0,
            hypothesis_id="H-1",
            parent_round=1,
            parent_commit=records[0].commit,
            outcome="inconclusive",
            comparison=MetricComparison.WITHIN_NOISE,
            baseline_metric=100.0,
            delta_pct=1.0,
        ),
    )
    hypothesis = state.by_id("H-1")
    assert hypothesis is not None
    assert hypothesis.measurement is not None
    assert hypothesis.measurement.delta_pct == 1.0
    assert hypothesis.resolution is HypothesisResolution.INCONCLUSIVE


def test_self_reported_improvement_never_resolves_proven() -> None:
    """Regression for #475: the agent's own number cannot prove its own claim.

    An implementer-reported round stores no comparison, so resolution has
    nothing to order and the hypothesis is unmeasured, not proven.
    """
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    state = HypothesisState(
        metrics=MetricSpace(objectives=(Objective(name="total_ops_per_sec", direction="max"),))
    )
    state = _run_round(
        search,
        state,
        [],
        hypothesis_id="H-base",
        round_number=1,
        record=_round(1, 100.0, hypothesis_id="H-base", provenance="implementer"),
    )
    records = state.rounds
    state = _run_round(
        search,
        state,
        records,
        hypothesis_id="H-1",
        round_number=2,
        record=_round(
            2,
            200.0,
            hypothesis_id="H-1",
            parent_round=1,
            parent_commit=records[0].commit,
            outcome="unmeasured",
            provenance="implementer",
            judge_verdict="pass",
        ),
    )
    hypothesis = state.by_id("H-1")
    assert hypothesis is not None
    assert hypothesis.measurement is None
    assert hypothesis.resolution is HypothesisResolution.UNMEASURED


def test_a_self_reported_baseline_never_backs_a_later_trusted_round() -> None:
    """A trusted round is never ordered against an untrusted baseline."""
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    state = HypothesisState(
        metrics=MetricSpace(objectives=(Objective(name="total_ops_per_sec", direction="max"),))
    )
    state = _run_round(
        search,
        state,
        [],
        hypothesis_id="H-base",
        round_number=1,
        record=_round(1, 100.0, hypothesis_id="H-base", provenance="implementer"),
    )
    records = state.rounds
    state = _run_round(
        search,
        state,
        records,
        hypothesis_id="H-1",
        round_number=2,
        record=_round(
            2,
            200.0,
            hypothesis_id="H-1",
            parent_round=1,
            parent_commit=records[0].commit,
            provenance="implementer",
            declared=None,
            judge_verdict="pass",
        ),
    )
    records = state.rounds
    state = _run_round(
        search,
        state,
        records,
        hypothesis_id="H-2",
        round_number=3,
        record=_round(
            3, 300.0, hypothesis_id="H-2", parent_round=2, parent_commit=records[1].commit
        ),
    )
    hypothesis = state.by_id("H-2")
    assert hypothesis is not None
    # Nothing trusted precedes round three, so its reading is incomparable,
    # not "better than the implementer's 200".
    assert hypothesis.resolution is HypothesisResolution.INCONCLUSIVE


def test_reprojection_of_a_self_reported_round_stays_unmeasured() -> None:
    """A resumed run must agree with the round record it wrote.

    An implementer round stores no comparison, which is otherwise
    indistinguishable from one written before the framework tracked
    provenance; the guard needs to be checked before the fallback would
    re-derive a comparison and disagree with the recorded outcome.
    """
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    state = HypothesisState(
        metrics=MetricSpace(objectives=(Objective(name="total_ops_per_sec", direction="max"),))
    )
    state = _run_round(
        search,
        state,
        [],
        hypothesis_id="H-base",
        round_number=1,
        record=_round(1, 100.0, hypothesis_id="H-base", provenance="implementer"),
    )
    records = state.rounds
    state = _run_round(
        search,
        state,
        records,
        hypothesis_id="H-1",
        round_number=2,
        record=_round(
            2,
            200.0,
            hypothesis_id="H-1",
            parent_round=1,
            parent_commit=records[0].commit,
            outcome="unmeasured",
            provenance="implementer",
            judge_verdict="pass",
        ),
    )
    # Serialize mid-sequence and continue from the deserialized copy: resume
    # must agree with the round record it wrote the first time around.
    resumed = HypothesisState.model_validate_json(state.model_dump_json())
    hypothesis = resumed.by_id("H-1")
    assert hypothesis is not None
    assert hypothesis.resolution is HypothesisResolution.UNMEASURED
    assert [record.hypothesis_outcome for record in hypothesis.rounds] == ["unmeasured"]


# --- frontier / best / pareto_conflict -----------------------------------


def test_frontier_never_dominated() -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="ops", direction="max"),
            Objective(name="latency", direction="min"),
        ),
        relative_noise=0.0,
    )
    records = [
        RoundRecord(
            round_number=n,
            commit=f"{n:040x}",
            passed=True,
            reviewed=True,
            hypothesis_id="H-1",
            hypothesis_outcome="proven",
            judge_verdict="pass",
            official_evaluation=True,
            perf_metric=ops,
            perf_unit="ops",
            perf_direction="max",
            perf_provenance="framework",
            metrics={"ops": ops, "latency": latency},
            candidate_metrics={"ops": ops, "latency": latency},
            candidate_disposition="pareto_frontier",
            candidate_retained=True,
        )
        for n, (ops, latency) in enumerate([(100, 50), (120, 60), (80, 30), (150, 70)], start=1)
    ]
    search = HypothesisSearch(HypothesisConfig(max_rounds=10))
    frontier = search.frontier(records, space=space)

    for candidate in frontier:
        for other in records:
            if other.round_number == candidate.round_number:
                continue
            assert not space.dominates(other.metrics, candidate.metrics)


def _official_round(number: int, ops: float, latency: float) -> RoundRecord:
    return RoundRecord(
        round_number=number,
        commit=f"{number:040x}",
        perf_metric=ops,
        perf_unit="ops_per_sec",
        passed=True,
        reviewed=True,
        hypothesis_id="H-1",
        hypothesis_outcome="proven",
        judge_verdict="pass",
        metrics={"ops_per_sec": ops, "latency_ms": latency},
        candidate_metrics={"ops_per_sec": ops, "latency_ms": latency},
        candidate_disposition="pareto_frontier",
        candidate_retained=True,
        official_evaluation=True,
        perf_direction="max",
        perf_provenance="framework",
    )


def test_frontier_and_best_select_the_non_dominated_winner() -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="ops_per_sec", direction="max"),
            Objective(name="latency_ms", direction="min"),
        ),
        relative_noise=0.02,
    )
    records = [_official_round(1, 100, 50), _official_round(2, 120, 40), _official_round(3, 90, 60)]

    search = HypothesisSearch(HypothesisConfig(max_rounds=10))
    frontier = search.frontier(records, space=space)
    assert [r.round_number for r in frontier] == [2]

    best = search.best(records, space=space)
    assert best is not None
    assert best.round_number == 2


def test_pareto_conflict_flags_a_dominated_frontier_claim() -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="ops_per_sec", direction="max"),
            Objective(name="latency_ms", direction="min"),
        ),
        relative_noise=0.02,
    )
    records = [_official_round(1, 100, 50), _official_round(2, 120, 40), _official_round(3, 90, 60)]
    search = HypothesisSearch(HypothesisConfig(max_rounds=10))

    conflict = search.pareto_conflict(
        disposition=CandidateDisposition.PARETO_FRONTIER,
        metrics={"ops_per_sec": 80, "latency_ms": 70},
        records=records,
        space=space,
    )
    assert conflict is not None


# --- start: strategy-update error paths ----------------------------------


def test_start_strategy_update_rejects_a_duplicate_hypothesis_id() -> None:
    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    state = _run_round(
        search,
        search.initial(),
        [],
        hypothesis_id="old",
        round_number=1,
        record=_round(1, 100.0, hypothesis_id="old"),
    )
    update = HypothesisStrategyUpdate(hypothesis_id="old", disposition="parked", reason="stop")
    with pytest.raises(ValueError, match="duplicate"):
        search.start(
            state,
            _plan("new", updates=[update, update]),
            round_number=2,
            parent_round=None,
            parent_commit=None,
        )


def test_start_strategy_update_rejects_an_incomplete_hypothesis() -> None:
    """A hypothesis with no rounds yet cannot be parked or abandoned.

    Every hypothesis the public lifecycle (``start``/``close_round``) ever
    produces either is still active or already carries a round, so this
    guard is built directly on ``HypothesisState``/``Hypothesis`` (both
    exported public types) to reach the one shape the lifecycle itself
    cannot: a completed-but-empty hypothesis.
    """
    incomplete = Hypothesis(hypothesis_id="incomplete", plan=_plan("incomplete"), started_round=1)
    state = HypothesisState(hypotheses=[incomplete])
    update = HypothesisStrategyUpdate(hypothesis_id="incomplete", disposition="parked", reason="x")

    search = HypothesisSearch(HypothesisConfig(max_rounds=5))
    with pytest.raises(ValueError, match="incomplete hypothesis"):
        search.start(
            state,
            _plan("next", updates=[update]),
            round_number=2,
            parent_round=None,
            parent_commit=None,
        )
