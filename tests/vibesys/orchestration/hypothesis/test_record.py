"""Round records separate agent claims from trusted gate measurements."""

from __future__ import annotations

from dataclasses import replace

from tests.support.declared_reply import DeclaredImplementerResponse

from vibesys.hypothesis import (
    AgentAttribution,
    CandidateDisposition,
    HypothesisConfig,
    HypothesisOutcome,
    HypothesisSearch,
    OrchestratorPlan,
    RoundRecord,
)
from vibesys.hypothesis.attempts import (
    AttemptState,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    PerformanceProjection,
)
from vibesys.hypothesis.readmodel import AgentRunProjection, project_hypothesis_state
from vibesys.hypothesis.record import RecordInput, build_round_record
from vibesys.hypothesis.state import Hypothesis, HypothesisState
from vibesys.metrics import FrameworkBenchmarkOutcome, MetricSpace, Objective
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.models import SingleAgentRoundResponse


def _record_input() -> RecordInput:
    plan = OrchestratorPlan(
        hypothesis_id="cache",
        task="cache responses",
        pass_criteria="behavior remains correct",  # noqa: S106  # LW-040062 [S106]; the argument is a fixture literal, not a credential.
        reasoning="reduce repeated work",
    )
    hypothesis = Hypothesis(
        hypothesis_id="cache",
        plan=plan,
        started_round=2,
        parent_round=1,
        parent_commit="parent-commit",
    )
    space = MetricSpace(objectives=(Objective("throughput", "max"),))
    state = HypothesisState(metrics=space, hypotheses=[hypothesis])
    parent = RoundRecord(
        round_number=1,
        commit="parent-commit",
        perf_metric=10.0,
        perf_unit="throughput",
        passed=True,
        metrics={"throughput": 10.0},
        official_evaluation=True,
        perf_provenance="framework",
    )
    attempt = AttemptState(
        agent_run_state=state,
        feedback=None,
        implementation=DeclaredImplementerResponse(
            summary="cache added",
            expected_behavior="higher throughput",
            declared=HypothesisOutcome.SUPPORTED.value,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
            candidate_metrics={"throughput": 12.0},
            candidate_evaluation_artifact="profile.json",
        ),
        judge=JudgeReviewed("pass"),
        passed=True,
        official_reason="final_round",
        framework_benchmark=FrameworkBenchmarkOutcome(
            metric_name="throughput",
            metric_value=12.0,
            metric_direction="max",
            row={"throughput": 12.0},
        ),
    )
    projection = PerformanceProjection(
        metric=12.0,
        unit="throughput",
        provenance="framework",
        profile_skipped=False,
        accepted_metrics={"throughput": 11.0},
        accepted_evaluation_artifact="benchmark.json",
        next_single_response=None,
    )
    return RecordInput(
        state=state,
        records=[parent],
        round_number=2,
        hypothesis=hypothesis,
        plan=plan,
        attempt=attempt,
        projection=projection,
        reviewed=True,
        framework_benchmark_configured=True,
        accuracy_configured=True,
        candidate_commit="candidate-commit",
        implementer=AgentAttribution(backend="cli", provider="codex", model="model-a"),
    )


def test_official_record_uses_benchmark_row_and_causal_parent() -> None:
    record = build_round_record(_record_input())

    assert record.official_evaluation
    assert record.metrics == {"throughput": 12.0}
    assert record.perf_baseline_round == 1
    assert record.perf_baseline_commit == "parent-commit"
    assert record.perf_baseline_metric == 10.0
    assert record.perf_delta_pct == 20.0
    assert record.candidate_retained
    assert record.implementer_provider == "codex"
    assert record.implementer_model == "model-a"


def test_official_status_needs_known_non_stub_attribution() -> None:
    data = _record_input()
    assert build_round_record(data).official_evaluation

    stub = AgentAttribution(backend="stub", provider=None, model=None)
    for implementer in (None, stub):
        record = build_round_record(replace(data, implementer=implementer))
        assert not record.official_evaluation
        assert record.implementer_model is None


def test_gate_retry_carries_approved_candidate_when_agent_omits_it() -> None:
    data = _record_input()
    data.hypothesis.gate_revalidation_pending = True
    data.hypothesis.gate_approved_candidate_disposition = CandidateDisposition.PARETO_FRONTIER.value
    data.hypothesis.gate_approved_candidate_metrics = {"throughput": 12.0}
    data.hypothesis.gate_approved_candidate_evaluation_artifact = "approved.json"
    assert isinstance(data.attempt.implementation, DeclaredImplementerResponse)
    data.attempt.implementation = data.attempt.implementation.model_copy(
        update={"candidate_disposition": CandidateDisposition.UNASSESSED}
    )
    data.attempt.passed = False

    record = build_round_record(replace(data, reviewed=False))
    assert not record.official_evaluation
    assert record.judge_verdict == "pass"
    assert record.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert record.candidate_metrics == {"throughput": 12.0}
    assert record.candidate_evaluation_artifact == "approved.json"
    assert record.candidate_retained is False


def test_failed_review_does_not_retain_agent_candidate() -> None:
    data = _record_input()
    data.attempt.passed = False
    data.attempt.judge = JudgeReviewed("fail")
    record = build_round_record(data)

    assert record.judge_verdict == "fail"
    assert not record.official_evaluation
    assert record.candidate_retained is False


def _unmeasured_candidate_input(
    disposition: CandidateDisposition,
    *,
    outcome: HypothesisOutcome = HypothesisOutcome.DISPROVEN,
) -> RecordInput:
    data = _record_input()
    assert isinstance(data.attempt.implementation, DeclaredImplementerResponse)
    data.attempt.implementation = data.attempt.implementation.model_copy(
        update={
            "declared": outcome,
            "candidate_disposition": disposition,
            "candidate_metrics": {"throughput": 12.0},
            "candidate_evaluation_artifact": "agent-profile.json",
            "candidate_operating_point": "agent-selected workload",
            "candidate_retention_reason": "agent reports a tradeoff",
        }
    )
    data.attempt.official_reason = None
    data.attempt.framework_benchmark = FrameworkBenchmarkOutcome()
    data.attempt.framework_perf_metric = None
    return replace(
        data,
        projection=PerformanceProjection(
            metric=12.0,
            unit="throughput",
            provenance="implementer",
            profile_skipped=False,
            accepted_metrics={"throughput": 12.0},
            accepted_evaluation_artifact="agent-profile.json",
            next_single_response=None,
        ),
        framework_benchmark_configured=False,
        accuracy_configured=False,
    )


def test_unmeasured_frontier_claim_cannot_retain_a_disproven_candidate() -> None:
    record = build_round_record(_unmeasured_candidate_input(CandidateDisposition.PARETO_FRONTIER))

    assert record.hypothesis_outcome == HypothesisOutcome.DISPROVEN.value
    assert record.candidate_retained is False
    assert record.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert record.candidate_metrics == {"throughput": 12.0}
    assert record.candidate_evaluation_artifact == "agent-profile.json"
    assert record.candidate_retention_reason == "agent reports a tradeoff"


def test_unmeasured_prerequisite_claim_cannot_retain_a_disproven_candidate() -> None:
    record = build_round_record(_unmeasured_candidate_input(CandidateDisposition.PREREQUISITE))

    assert record.hypothesis_outcome == HypothesisOutcome.DISPROVEN.value
    assert record.candidate_retained is False
    assert record.candidate_disposition == CandidateDisposition.PREREQUISITE.value


def test_deferred_frontier_claim_cannot_create_a_retention_decision() -> None:
    data = _unmeasured_candidate_input(CandidateDisposition.PARETO_FRONTIER)
    data.attempt.judge = JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)

    record = build_round_record(replace(data, reviewed=False))

    assert record.passed
    assert record.judge_verdict == "deferred"
    assert not record.reviewed
    assert record.candidate_retained is False


def test_trusted_frontier_measurement_can_retain_a_disproven_hypothesis_candidate() -> None:
    data = _record_input()
    assert isinstance(data.attempt.implementation, DeclaredImplementerResponse)
    data.attempt.implementation = data.attempt.implementation.model_copy(
        update={"declared": HypothesisOutcome.DISPROVEN}
    )

    record = build_round_record(data)

    assert record.hypothesis_outcome == HypothesisOutcome.DISPROVEN.value
    assert record.perf_provenance == "framework"
    assert record.candidate_retained is True


def test_unmeasured_retention_stays_false_across_resume_and_read_model_projection() -> None:
    data = _unmeasured_candidate_input(CandidateDisposition.PARETO_FRONTIER)
    record = build_round_record(data)
    stored_hypothesis = data.hypothesis.model_copy(update={"rounds": [record]}, deep=True)
    stored = HypothesisState(metrics=data.state.metrics, hypotheses=[stored_hypothesis])

    resumed = HypothesisSearch(HypothesisConfig(max_rounds=2)).resume(stored, stored.metrics)
    projected = AgentRunProjection.model_validate(project_hypothesis_state(resumed).payload)

    hypothesis = resumed.by_id(data.hypothesis.hypothesis_id)
    assert hypothesis is not None
    assert hypothesis.candidate_retained is False
    assert projected.hypotheses[0].best is False


def test_single_and_multi_replies_share_unmeasured_retention_policy() -> None:
    multi_data = _unmeasured_candidate_input(CandidateDisposition.PARETO_FRONTIER)
    assert multi_data.attempt.implementation is not None
    response = multi_data.attempt.implementation
    single_response = SingleAgentRoundResponse(
        summary="cache added",
        expected_behavior="higher throughput",
        self_review="reviewed",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
        perf_metric=12.0,
        perf_unit="throughput",
        candidate_disposition=response.candidate_disposition,
        candidate_metrics=response.candidate_metrics,
        candidate_evaluation_artifact=response.candidate_evaluation_artifact,
        candidate_operating_point=response.candidate_operating_point,
        candidate_retention_reason=response.candidate_retention_reason,
    )
    single_attempt = replace(
        multi_data.attempt,
        implementation=None,
        single_agent_response=single_response,
    )
    single_data = replace(multi_data, attempt=single_attempt)

    multi_record = build_round_record(multi_data)
    single_record = build_round_record(single_data)

    assert multi_record.candidate_retained is False
    assert single_record.candidate_retained is False
    assert single_record.candidate_disposition == multi_record.candidate_disposition
    assert single_record.candidate_metrics == multi_record.candidate_metrics


# --- causal baseline selection (build_round_record -> metric_baseline) -----
#
# These drive the same fallback/fail-closed/renamed-unit/untrusted-provenance
# paths that ``metric_baseline`` implements, entirely through the record
# builder's public ``RecordInput``/``build_round_record`` surface: no
# ``vibesys.hypothesis.transitions`` import needed.


def _official(number: int, metric: float, *, unit: str = "throughput", provenance="framework"):  # noqa: ANN001, ANN202  # LW-040063 [ANN001, ANN202]; this scripted double mirrors a production signature whose parameters are not annotated here. The helper is private to this test module and its return type is the local closure type.
    return RoundRecord(
        round_number=number,
        commit=f"parent-commit-{number}",
        perf_metric=metric,
        perf_unit=unit,
        passed=True,
        metrics={unit: metric},
        official_evaluation=True,
        perf_provenance=provenance,
    )


def test_baseline_falls_back_to_the_newest_official_reading_bounded_by_parent_round() -> None:
    data = _record_input()
    parent = _official(1, 10.0)
    later = _official(2, 20.0)
    data = replace(data, records=[parent, later])
    data.hypothesis.parent_round = 2
    data.hypothesis.parent_commit = "unmatched-commit"

    record = build_round_record(data)

    assert record.perf_baseline_round == 2
    assert record.perf_baseline_commit == later.commit
    assert record.perf_baseline_metric == 20.0


def test_baseline_fails_closed_with_an_unplaced_parent_commit_and_no_round_bound() -> None:
    data = _record_input()
    data = replace(data, records=[_official(1, 10.0)])
    data.hypothesis.parent_round = None
    data.hypothesis.parent_commit = "a commit no official round carries"

    record = build_round_record(data)

    assert record.perf_baseline_round is None
    assert record.perf_baseline_commit is None
    assert record.perf_baseline_metric is None
    assert record.perf_delta_pct is None


def test_baseline_matches_a_renamed_headline_unit_through_its_metrics_row() -> None:
    """A prior round whose *displayed* unit was renamed still matches, via its
    metrics row keyed by the current objective name ("throughput" here).
    """
    data = _record_input()
    renamed = RoundRecord(
        round_number=1,
        commit="renamed-commit",
        perf_metric=10.0,
        perf_unit="old_headline_name",
        passed=True,
        metrics={"throughput": 10.0},
        official_evaluation=True,
        perf_provenance="framework",
    )
    data = replace(data, records=[renamed])
    data.hypothesis.parent_round = 1
    data.hypothesis.parent_commit = renamed.commit

    record = build_round_record(data)

    assert record.perf_baseline_round == 1
    assert record.perf_baseline_metric == 10.0


def test_baseline_skips_an_agent_self_reported_prior_round() -> None:
    data = _record_input()
    self_reported = _official(1, 999.0, provenance="implementer")
    data = replace(data, records=[self_reported])
    data.hypothesis.parent_round = 1
    data.hypothesis.parent_commit = self_reported.commit

    record = build_round_record(data)

    assert record.perf_baseline_round is None
    assert record.perf_baseline_metric is None
