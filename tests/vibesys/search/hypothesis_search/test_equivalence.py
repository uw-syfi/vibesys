"""Equivalence tests: old ``agent_run``/``loops`` logic vs. ``search.hypothesis``.

The rewiring phase deletes ``vibesys.agent_run`` and the shared logic in
``loops/{multi,profile_multi}/decisions.py`` once every strategy is wired
onto ``search.hypothesis``. Until then, these tests are the contract: both
implementations must agree on every input. Once the old code is deleted,
each ``test_*_matches_old_*`` here should be trimmed to a plain unit test of
the new function alone (drop the old-side call and the equality assertion
against it).
"""

from __future__ import annotations

from typing import Literal

from hypothesis import given
from hypothesis import strategies as st

import vibesys.agent_run.evidence as old_evidence
import vibesys.agent_run.hypotheses as old_hypotheses
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.metrics import MetricComparison, MetricSpace, Objective
from vibesys.loops.multi.decisions import (
    candidate_evidence_is_fresh as old_candidate_evidence_is_fresh,
)
from vibesys.loops.multi.decisions import (
    official_evaluation_reason as old_official_evaluation_reason,
)
from vibesys.loops.multi.decisions import review_due as old_review_due
from vibesys.roles.implementer import ImplementerResponse
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    OrchestratorPlan,
)
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch
from vibesys.search.hypothesis import transitions as new_transitions
from vibesys.search.hypothesis.cadence import (
    candidate_evidence_fresh as new_candidate_evidence_fresh,
)
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
def test_resolve_hypothesis_outcome_matches_old(
    declared: HypothesisOutcome | None,
    passed: bool,  # noqa: FBT001
    reviewed: bool,  # noqa: FBT001
    comparison: MetricComparison | None,
) -> None:
    old = old_hypotheses.resolve_hypothesis_outcome(
        old_hypotheses.ResolutionEvidence(
            declared=declared, passed=passed, reviewed=reviewed, comparison=comparison
        )
    )
    new = new_transitions.resolve_hypothesis_outcome(
        new_transitions.ResolutionEvidence(
            declared=declared, passed=passed, reviewed=reviewed, comparison=comparison
        )
    )
    assert old == new


@given(comparison=st.sampled_from(list(MetricComparison)))
def test_scalar_candidate_retained_matches_old(comparison: MetricComparison) -> None:
    assert old_hypotheses.scalar_candidate_retained(
        comparison
    ) == new_transitions.scalar_candidate_retained(comparison)


def test_trusted_perf_provenance_matches_old() -> None:
    for provenance in (None, "framework", "implementer"):
        assert old_hypotheses.trusted_perf_provenance(
            provenance
        ) == new_transitions.trusted_perf_provenance(provenance)


# --- start_hypothesis / append_round / project_round_evidence round trip ---


def test_start_and_append_round_matches_old_agent_run_state() -> None:
    plan = _plan("H-1")
    old_state = old_hypotheses.start_hypothesis(AgentRunState(), plan, started_round=1)
    new_state = new_transitions.start_hypothesis(HypothesisState(), plan, started_round=1)
    assert old_state.model_dump() == new_state.model_dump()

    record = _round(1, 12.0)
    old_next = old_hypotheses.append_round(old_state, record, keep_active=False)
    new_next = new_transitions.append_round(new_state, record, keep_active=False)
    assert old_next.model_dump() == new_next.model_dump()


@given(
    metric=st.one_of(st.none(), st.floats(min_value=0.1, max_value=1000, allow_nan=False)),
    outcome=st.sampled_from(["proven", "disproven", "inconclusive", "blocked", "continue"]),
    declared=st.sampled_from([None, "nominated", "supported", "continue", "disproven"]),
    passed=st.booleans(),
    reviewed=st.booleans(),
    judge_verdict=st.sampled_from([None, "pass", "fail", "deferred"]),
)
def test_project_round_evidence_matches_old(  # noqa: PLR0913
    metric: float | None,
    outcome: str,
    declared: str | None,
    passed: bool,  # noqa: FBT001
    reviewed: bool,  # noqa: FBT001
    judge_verdict: Literal["pass", "fail", "deferred"] | None,
) -> None:
    plan = _plan("H-1")
    old_state = old_hypotheses.start_hypothesis(AgentRunState(), plan, started_round=1)
    new_state = new_transitions.start_hypothesis(HypothesisState(), plan, started_round=1)
    record = _round(
        1,
        metric,
        outcome=outcome,
        declared=declared,
        passed=passed,
        reviewed=reviewed,
        judge_verdict=judge_verdict,
    )
    old_active = old_state.active_hypothesis
    new_active = new_state.active_hypothesis
    assert old_active is not None
    assert new_active is not None
    old_hypothesis = old_hypotheses.project_round_evidence(
        old_active, record, prior_rounds=[], space=old_state.metrics
    )
    new_hypothesis = new_transitions.project_round_evidence(
        new_active, record, prior_rounds=[], space=new_state.metrics
    )
    assert old_hypothesis.model_dump() == new_hypothesis.model_dump()


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


def test_frontier_and_best_match_old_evidence_helpers() -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="ops_per_sec", direction="max"),
            Objective(name="latency_ms", direction="min"),
        ),
        relative_noise=0.02,
    )
    records = [_official_round(1, 100, 50), _official_round(2, 120, 40), _official_round(3, 90, 60)]

    old_frontier = old_evidence._pareto_frontier_records(records, space)  # noqa: SLF001
    new_frontier = new_transitions.pareto_frontier_records(records, space)
    assert [r.round_number for r in old_frontier] == [r.round_number for r in new_frontier]

    old_best = old_evidence._select_final_candidate(records, space)  # noqa: SLF001
    new_best = new_transitions.select_final_candidate(records, space)
    assert (old_best.round_number if old_best else None) == (
        new_best.round_number if new_best else None
    )

    old_conflict = old_evidence._pareto_archive_conflict(  # noqa: SLF001
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"ops_per_sec": 80, "latency_ms": 70},
        records=records,
        space=space,
    )
    new_conflict = new_transitions.pareto_archive_conflict(
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"ops_per_sec": 80, "latency_ms": 70},
        records=records,
        space=space,
    )
    assert old_conflict == new_conflict


# --- review_due / official_due / candidate_evidence_fresh cadence ---


@given(
    round_number=st.integers(min_value=1, max_value=20),
    max_rounds=st.integers(min_value=1, max_value=20),
    judge_every=st.integers(min_value=1, max_value=5),
    outcome=st.sampled_from(list(HypothesisOutcome)),
    candidate_evidence_fresh=st.booleans(),
)
def test_review_due_matches_old(
    round_number: int,
    max_rounds: int,
    judge_every: int,
    outcome: HypothesisOutcome,
    candidate_evidence_fresh: bool,  # noqa: FBT001
) -> None:
    old = old_review_due(
        round_number=round_number,
        max_rounds=max_rounds,
        judge_every=judge_every,
        outcome=outcome,
        candidate_evidence_fresh=candidate_evidence_fresh,
    )
    search = HypothesisSearch(HypothesisConfig(max_rounds=max_rounds, judge_every=judge_every))
    new = search.review_due(
        round_number=round_number,
        outcome=outcome,
        candidate_evidence_is_fresh=candidate_evidence_fresh,
    )
    assert old == new


@given(
    round_number=st.integers(min_value=1, max_value=10),
    max_rounds=st.integers(min_value=1, max_value=10),
    official_eval_every=st.integers(min_value=1, max_value=5),
    requested=st.booleans(),
    candidate_ready=st.booleans(),
    provisional=st.integers(min_value=0, max_value=5),
)
def test_official_due_matches_old(  # noqa: PLR0913
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
    old = old_official_evaluation_reason(
        records=records,
        round_number=round_number,
        max_rounds=max_rounds,
        official_eval_every=official_eval_every,
        requested=requested,
        candidate_ready=candidate_ready,
    )
    search = HypothesisSearch(
        HypothesisConfig(max_rounds=max_rounds, official_eval_every=official_eval_every)
    )
    new = search.official_due(
        records=records,
        round_number=round_number,
        requested=requested,
        candidate_ready=candidate_ready,
    )
    assert old == new


def test_candidate_evidence_fresh_matches_old() -> None:
    candidate_metrics = {"a": 1.0}
    candidate_evaluation_artifact = "artifact-1"
    implementation = ImplementerResponse(
        summary="did the thing",
        expected_behavior="it works",
        candidate_metrics=candidate_metrics,
        candidate_evaluation_artifact=candidate_evaluation_artifact,
    )

    records: list[RoundRecord] = []
    assert old_candidate_evidence_is_fresh(implementation, records) == new_candidate_evidence_fresh(
        candidate_metrics=candidate_metrics,
        candidate_evaluation_artifact=candidate_evaluation_artifact,
        records=records,
    )
