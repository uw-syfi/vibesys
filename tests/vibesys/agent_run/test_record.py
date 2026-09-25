"""Round records separate agent claims from trusted gate measurements."""

from __future__ import annotations

from dataclasses import replace

from tests.support import make_orchestrator_plan

from vibesys.agent_run.attempts import (
    AttemptState,
    JudgeReviewed,
    PerformanceProjection,
)
from vibesys.agent_run.record import RecordInput, build_round_record
from vibesys.agent_run.state import AgentRunState, Hypothesis
from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    ImplementerResponse,
    Verdict,
)
from vs_loop_state.api import RoundRecord


def _record_input() -> RecordInput:
    plan = make_orchestrator_plan(
        hypothesis_id="cache",
        task="cache responses",
        criteria="behavior remains correct",
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
    state = AgentRunState(metrics=space, hypotheses=[hypothesis])
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
        implementation=ImplementerResponse(
            summary="cache added",
            expected_behavior="higher throughput",
            hypothesis_outcome=HypothesisOutcome.SUPPORTED,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
            candidate_metrics={"throughput": 12.0},
            candidate_evaluation_artifact="profile.json",
        ),
        judge=JudgeReviewed(Verdict.PASS.value),
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
        backend_name="cli",
        driver_name="agentshim",
        provider="codex",
        model="model-a",
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
    assert record.implementer_driver == "agentshim"
    assert record.implementer_provider == "codex"
    assert record.implementer_model == "model-a"


def test_gate_retry_carries_approved_candidate_when_agent_omits_it() -> None:
    data = _record_input()
    data.hypothesis.gate_revalidation_pending = True
    data.hypothesis.gate_approved_candidate_disposition = CandidateDisposition.PARETO_FRONTIER.value
    data.hypothesis.gate_approved_candidate_metrics = {"throughput": 12.0}
    data.hypothesis.gate_approved_candidate_evaluation_artifact = "approved.json"
    assert data.attempt.implementation is not None
    data.attempt.implementation.candidate_disposition = CandidateDisposition.UNASSESSED
    data.attempt.passed = False

    record = build_round_record(replace(data, reviewed=False))
    assert not record.official_evaluation
    assert record.judge_verdict == "pass"
    assert record.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert record.candidate_metrics == {"throughput": 12.0}
    assert record.candidate_evaluation_artifact == "approved.json"
    assert record.candidate_retained


def test_failed_review_does_not_retain_agent_candidate() -> None:
    data = _record_input()
    data.attempt.passed = False
    data.attempt.judge = JudgeReviewed(Verdict.FAIL.value)
    record = build_round_record(data)

    assert record.judge_verdict == "fail"
    assert not record.official_evaluation
    assert record.candidate_retained is False
