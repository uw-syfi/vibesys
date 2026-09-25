"""Property tests for the pure hypothesis-lifecycle transitions and cadence.

Formerly cross-checked the ported ``search.hypothesis`` logic against the
original ``vibesys.agent_run`` implementation it was ported from; agent_run
has since dissolved (its logic now lives only in ``search.hypothesis``), so
these are plain property tests of that logic alone.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import given
from hypothesis import strategies as st

from vibesys.evaluators.metrics import MetricComparison, MetricSpace, Objective
from vibesys.schemas import CandidateDisposition, HypothesisOutcome
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch, transitions
from vibesys.search.hypothesis.cadence import candidate_evidence_fresh
from vibesys.search.hypothesis.plan import OrchestratorPlan
from vibesys.search.hypothesis.state import HypothesisState
from vs_loop_state.api import RoundRecord


def _plan(identifier: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        task=f"implement {identifier}",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="test the claim",
    )


def _round(  # noqa: PLR0913
    number: int,
    metric: float | None,
    *,
    hypothesis_id: str = "H-1",
    parent_round: int | None = None,
    parent_commit: str | None = None,
    outcome: str = "proven",
    declared: str | None = "nominated",
    direction: Literal["max", "min"] = "max",
    retained: bool | None = True,
    comparison: MetricComparison | None = None,
    passed: bool = True,
    reviewed: bool = True,
    judge_verdict: Literal["pass", "fail", "deferred"] | None = "pass",
) -> RoundRecord:
    return RoundRecord(
        round_number=number,
        commit=f"{number:040x}",
        perf_metric=metric,
        perf_unit="total_ops_per_sec" if metric is not None else None,
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
        metrics={"total_ops_per_sec": metric} if metric is not None else {},
        official_evaluation=metric is not None,
        perf_direction=direction if metric is not None else None,
        candidate_retained=retained,
        perf_comparison=comparison,
        perf_provenance="framework" if metric is not None else None,
    )


# --- resolve_hypothesis_outcome ---

_OUTCOMES = st.sampled_from([None, *HypothesisOutcome])
_COMPARISONS = st.sampled_from([None, *MetricComparison])


@given(
    declared=_OUTCOMES,
    passed=st.booleans(),
    reviewed=st.booleans(),
    comparison=_COMPARISONS,
)
def test_resolve_hypothesis_outcome(
    declared: HypothesisOutcome | None,
    passed: bool,  # noqa: FBT001
    reviewed: bool,  # noqa: FBT001
    comparison: MetricComparison | None,
) -> None:
    result = transitions.resolve_hypothesis_outcome(
        transitions.ResolutionEvidence(
            declared=declared, passed=passed, reviewed=reviewed, comparison=comparison
        )
    )
    if not reviewed:
        assert result is None
    elif not passed:
        assert result is transitions.HypothesisResolution.REJECTED
    elif declared is None or declared is HypothesisOutcome.CONTINUE:
        assert result is None


@given(comparison=st.sampled_from(list(MetricComparison)))
def test_scalar_candidate_retained(comparison: MetricComparison) -> None:
    result = transitions.scalar_candidate_retained(comparison)
    match comparison:
        case MetricComparison.BETTER:
            assert result is True
        case MetricComparison.WORSE | MetricComparison.WITHIN_NOISE:
            assert result is False
        case MetricComparison.INCOMPARABLE:
            assert result is None


def test_trusted_perf_provenance() -> None:
    assert transitions.trusted_perf_provenance(None)
    assert transitions.trusted_perf_provenance("framework")
    assert not transitions.trusted_perf_provenance("implementer")


# --- start_hypothesis / append_round / project_round_evidence round trip ---


def test_start_and_append_round() -> None:
    plan = _plan("H-1")
    state = transitions.start_hypothesis(HypothesisState(), plan, started_round=1)
    assert state.active_hypothesis is not None
    assert state.active_hypothesis.hypothesis_id == "H-1"

    record = _round(1, 12.0)
    next_state = transitions.append_round(state, record, keep_active=False)
    assert next_state.active_hypothesis_id is None
    next_hypothesis = next_state.by_id("H-1")
    assert next_hypothesis is not None
    assert next_hypothesis.rounds[0].round_number == 1


@given(
    metric=st.one_of(st.none(), st.floats(min_value=0.1, max_value=1000, allow_nan=False)),
    outcome=st.sampled_from(["proven", "disproven", "inconclusive", "blocked", "continue"]),
    declared=st.sampled_from([None, "nominated", "supported", "continue", "disproven"]),
    passed=st.booleans(),
    reviewed=st.booleans(),
    judge_verdict=st.sampled_from([None, "pass", "fail", "deferred"]),
)
def test_project_round_evidence(  # noqa: PLR0913
    metric: float | None,
    outcome: str,
    declared: str | None,
    passed: bool,  # noqa: FBT001
    reviewed: bool,  # noqa: FBT001
    judge_verdict: Literal["pass", "fail", "deferred"] | None,
) -> None:
    plan = _plan("H-1")
    state = transitions.start_hypothesis(HypothesisState(), plan, started_round=1)
    record = _round(
        1,
        metric,
        outcome=outcome,
        declared=declared,
        passed=passed,
        reviewed=reviewed,
        judge_verdict=judge_verdict,
    )
    active = state.active_hypothesis
    assert active is not None
    projected = transitions.project_round_evidence(
        active, record, prior_rounds=[], space=state.metrics
    )
    assert projected.rounds[-1].round_number == 1


# --- frontier / best / pareto_conflict ---


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


def test_frontier_and_best_evidence_helpers() -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="ops_per_sec", direction="max"),
            Objective(name="latency_ms", direction="min"),
        ),
        relative_noise=0.02,
    )
    records = [_official_round(1, 100, 50), _official_round(2, 120, 40), _official_round(3, 90, 60)]

    frontier = transitions.pareto_frontier_records(records, space)
    assert [r.round_number for r in frontier] == [2]

    best = transitions.select_final_candidate(records, space)
    assert best is not None
    assert best.round_number == 2

    conflict = transitions.pareto_archive_conflict(
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"ops_per_sec": 80, "latency_ms": 70},
        records=records,
        space=space,
    )
    assert conflict is not None


# --- review_due / official_due / candidate_evidence_fresh cadence ---


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
    candidate_evidence_fresh: bool,  # noqa: FBT001
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


@given(
    round_number=st.integers(min_value=1, max_value=10),
    max_rounds=st.integers(min_value=1, max_value=10),
    official_eval_every=st.integers(min_value=1, max_value=5),
    requested=st.booleans(),
    candidate_ready=st.booleans(),
    provisional=st.integers(min_value=0, max_value=5),
)
def test_official_due_matches_expected_cadence(  # noqa: PLR0913
    round_number: int,
    max_rounds: int,
    official_eval_every: int,
    requested: bool,  # noqa: FBT001
    candidate_ready: bool,  # noqa: FBT001
    provisional: int,
) -> None:
    records = [
        _round(
            n, None, outcome="continue", declared="continue", judge_verdict="pass", retained=True
        )
        for n in range(1, provisional + 1)
    ]
    search = HypothesisSearch(
        HypothesisConfig(max_rounds=max_rounds, official_eval_every=official_eval_every)
    )
    reason = search.official_due(
        records=records,
        round_number=round_number,
        requested=requested,
        candidate_ready=candidate_ready,
    )
    if round_number == max_rounds:
        assert reason == "final_round"
    elif not candidate_ready:
        assert reason is None
    elif requested:
        assert reason == "orchestrator_request"
    elif provisional + 1 >= official_eval_every:
        assert reason == "cadence"
    else:
        assert reason is None


def test_candidate_evidence_fresh_requires_metrics_and_detects_new_artifacts() -> None:
    candidate_metrics = {"a": 1.0}
    candidate_evaluation_artifact = "artifact-1"

    assert not candidate_evidence_fresh(
        candidate_metrics={}, candidate_evaluation_artifact=None, records=[]
    )
    assert candidate_evidence_fresh(
        candidate_metrics=candidate_metrics,
        candidate_evaluation_artifact=candidate_evaluation_artifact,
        records=[],
    )
