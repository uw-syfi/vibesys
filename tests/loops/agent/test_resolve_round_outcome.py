# ruff: noqa: ANN001, ANN201, PLR0913, S106
"""Unit tests for _resolve_round_outcome (issue #290)."""

from vibesys.loops.agent.loop import (
    _ActiveHypothesis,
    _resolve_round_outcome,
)
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    ImplementerResponse,
    OrchestratorPlan,
    SingleAgentRoundResponse,
    Verdict,
)


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(task="t", pass_criteria="p", hypothesis_id="h-1", reasoning="r")


def _hypothesis() -> _ActiveHypothesis:
    return _ActiveHypothesis(plan=_plan(), started_round=1)


def _implementer(
    *,
    outcome=HypothesisOutcome.SUPPORTED,
    perf_metric=None,
    perf_unit=None,
    metrics=None,
    evaluation_artifact=None,
    candidate_disposition=CandidateDisposition.UNASSESSED,
    candidate_metrics=None,
    candidate_evaluation_artifact=None,
    candidate_operating_point="",
    candidate_retention_reason="",
) -> ImplementerResponse:
    return ImplementerResponse(
        summary="s",
        expected_behavior="b",
        hypothesis_outcome=outcome,
        evidence="e",
        next_step="",
        perf_metric=perf_metric,
        perf_unit=perf_unit,
        metrics=metrics or {},
        evaluation_artifact=evaluation_artifact,
        candidate_disposition=candidate_disposition,
        candidate_metrics=candidate_metrics or {},
        candidate_evaluation_artifact=candidate_evaluation_artifact,
        candidate_operating_point=candidate_operating_point,
        candidate_retention_reason=candidate_retention_reason,
    )


def _single_agent(
    *,
    verdict=Verdict.PASS,
    perf_metric=None,
    perf_unit=None,
    candidate_disposition=CandidateDisposition.UNASSESSED,
    candidate_metrics=None,
) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="s",
        expected_behavior="b",
        self_review="r",
        feedback="",
        verdict=verdict,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
        perf_metric=perf_metric,
        perf_unit=perf_unit,
        candidate_disposition=candidate_disposition,
        candidate_metrics=candidate_metrics or {},
    )


def test_single_agent_pass_with_official_metric():
    response = _single_agent()
    outcome = _resolve_round_outcome(
        inner_loop="single-agent",
        passed=True,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason="cadence",
        framework_perf_metric=12.5,
        benchmark_result=None,
        implementation=None,
        single_agent_response=response,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.perf_metric == 12.5
    assert outcome.profile_skipped is False
    assert outcome.hypothesis_outcome == "proven"
    assert outcome.reviewed is True
    assert outcome.last_single_agent_response is response


def test_single_agent_no_response_profile_skipped():
    outcome = _resolve_round_outcome(
        inner_loop="single-agent",
        passed=False,
        final_attempt_reviewed=False,
        completed_official_evaluation_reason=None,
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=None,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.profile_skipped is True
    assert outcome.perf_metric is None
    assert outcome.last_single_agent_response is None
    assert outcome.hypothesis_outcome == "rejected"


def test_single_agent_not_passed_but_reviewed_is_rejected():
    response = _single_agent(verdict=Verdict.FAIL)
    outcome = _resolve_round_outcome(
        inner_loop="single-agent",
        passed=False,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason=None,
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=None,
        single_agent_response=response,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.hypothesis_outcome == "rejected"
    assert outcome.reviewed is True


def test_multi_agent_framework_perf_metric_wins():
    implementation = _implementer(perf_metric=1.0, perf_unit="tok/s")
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=True,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason="cadence",
        framework_perf_metric=99.0,
        benchmark_result=None,
        implementation=implementation,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.perf_metric == 99.0
    assert outcome.accepted_metrics == {}
    assert outcome.accepted_evaluation_artifact is None


def test_multi_agent_implementation_metric_used_when_no_framework_metric():
    implementation = _implementer(
        perf_metric=5.0,
        perf_unit="ms",
        metrics={"latency_ms": 5.0},
        evaluation_artifact="artifact.json",
    )
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=True,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason="cadence",
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=implementation,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.perf_metric == 5.0
    assert outcome.perf_unit == "ms"
    assert outcome.accepted_metrics == {"latency_ms": 5.0}
    assert outcome.accepted_evaluation_artifact == "artifact.json"
    assert outcome.profile_skipped is False


def test_multi_agent_gate_revalidation_perf_fallback():
    hyp = _hypothesis()
    hyp.gate_revalidation_pending = True
    hyp.gate_approved_perf_metric = 4.0
    hyp.gate_approved_perf_unit = "ms"
    hyp.gate_approved_metrics = {"latency_ms": 4.0}
    hyp.gate_approved_evaluation_artifact = "prior.json"
    implementation = _implementer(outcome=HypothesisOutcome.SUPPORTED, perf_metric=None)
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=True,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason="cadence",
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=implementation,
        single_agent_response=None,
        active_hypothesis=hyp,
    )
    assert outcome.perf_metric == 4.0
    assert outcome.perf_unit == "ms"
    assert outcome.accepted_metrics == {"latency_ms": 4.0}
    assert outcome.accepted_evaluation_artifact == "prior.json"


def test_multi_agent_no_metric_at_all():
    implementation = _implementer(perf_metric=None)
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=False,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason=None,
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=implementation,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.perf_metric is None
    assert outcome.perf_unit is None
    assert outcome.profile_skipped is True
    assert outcome.accepted_metrics == {}
    assert outcome.accepted_evaluation_artifact is None


def test_multi_agent_passed_nominated_labeled_proven():
    implementation = _implementer(outcome=HypothesisOutcome.NOMINATED)
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=True,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason="cadence",
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=implementation,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.hypothesis_outcome == "proven"


def test_multi_agent_not_reviewed_uses_implementation_label():
    implementation = _implementer(outcome=HypothesisOutcome.CONTINUE)
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=False,
        final_attempt_reviewed=False,
        completed_official_evaluation_reason=None,
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=implementation,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.hypothesis_outcome == HypothesisOutcome.CONTINUE.value
    assert outcome.reviewed is False


def test_no_implementation_and_not_passed_gives_none_label():
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=False,
        final_attempt_reviewed=False,
        completed_official_evaluation_reason=None,
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=None,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.hypothesis_outcome is None


def test_candidate_evidence_from_implementation():
    implementation = _implementer(
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"latency_ms": 3.0},
        candidate_evaluation_artifact="c.json",
        candidate_operating_point="op",
        candidate_retention_reason="reason",
    )
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=True,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason="cadence",
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=implementation,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert outcome.candidate_metrics == {"latency_ms": 3.0}
    assert outcome.candidate_evaluation_artifact == "c.json"
    assert outcome.candidate_operating_point == "op"
    assert outcome.candidate_retention_reason == "reason"


def test_candidate_evidence_from_single_agent_response():
    response = _single_agent(
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"tok_s": 10.0},
    )
    outcome = _resolve_round_outcome(
        inner_loop="single-agent",
        passed=True,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason="cadence",
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=None,
        single_agent_response=response,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert outcome.candidate_metrics == {"tok_s": 10.0}


def test_candidate_evidence_defaults_when_nothing_present():
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=False,
        final_attempt_reviewed=False,
        completed_official_evaluation_reason=None,
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=None,
        single_agent_response=None,
        active_hypothesis=_hypothesis(),
    )
    assert outcome.candidate_disposition == CandidateDisposition.UNASSESSED.value
    assert outcome.candidate_metrics == {}
    assert outcome.candidate_evaluation_artifact is None
    assert outcome.candidate_operating_point == ""
    assert outcome.candidate_retention_reason == ""


def test_candidate_evidence_falls_back_to_gate_approved_frontier():
    hyp = _hypothesis()
    hyp.gate_revalidation_pending = True
    hyp.gate_approved_candidate_disposition = CandidateDisposition.PARETO_FRONTIER.value
    hyp.gate_approved_candidate_metrics = {"latency_ms": 2.0}
    hyp.gate_approved_candidate_evaluation_artifact = "gate.json"
    hyp.gate_approved_candidate_operating_point = "gate-op"
    hyp.gate_approved_candidate_retention_reason = "gate-reason"
    outcome = _resolve_round_outcome(
        inner_loop="multi-agent",
        passed=False,
        final_attempt_reviewed=True,
        completed_official_evaluation_reason=None,
        framework_perf_metric=None,
        benchmark_result=None,
        implementation=None,
        single_agent_response=None,
        active_hypothesis=hyp,
    )
    assert outcome.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert outcome.candidate_metrics == {"latency_ms": 2.0}
    assert outcome.candidate_evaluation_artifact == "gate.json"
    assert outcome.candidate_operating_point == "gate-op"
    assert outcome.candidate_retention_reason == "gate-reason"
