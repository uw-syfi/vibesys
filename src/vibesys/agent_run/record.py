"""Build one agent round record from final attempt and trusted gate evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vibesys.agent_run.attempts import recorded_judge_verdict
from vibesys.agent_run.evidence import (
    _pareto_archive_dominators,
    _provisional_candidate_retained,
)
from vibesys.agent_run.hypotheses import (
    ResolutionEvidence,
    metric_baseline,
    record_metric_value,
    resolve_hypothesis_outcome,
    scalar_candidate_retained,
    trusted_perf_provenance,
)
from vibesys.evaluators.metrics import Measurement
from vibesys.schemas import CandidateDisposition, HypothesisOutcome
from vs_loop_state.api import RoundRecord

if TYPE_CHECKING:
    from vibesys.agent_run.attempts import AttemptState, PerformanceProjection
    from vibesys.agent_run.state import AgentRunState, Hypothesis
    from vibesys.schemas import OrchestratorPlan
    from vs_loop_state.api import MetricComparison


@dataclass(frozen=True)
class RecordInput:
    """Authoritative final-attempt facts for one completed round."""

    state: AgentRunState
    records: list[RoundRecord]
    round_number: int
    hypothesis: Hypothesis
    plan: OrchestratorPlan
    attempt: AttemptState
    projection: PerformanceProjection
    reviewed: bool
    framework_benchmark_configured: bool
    accuracy_configured: bool
    candidate_commit: str | None
    backend_name: str
    driver_name: str | None
    provider: str | None
    model: str | None


@dataclass(frozen=True)
class CandidateEvidence:
    """Provisional candidate fields, with approved evidence on gate retry."""

    disposition: str
    metrics: dict[str, float]
    artifact: str | None
    operating_point: str
    retention_reason: str


@dataclass(frozen=True)
class MeasurementEvidence:
    """One trusted headline reading and its causal baseline."""

    accepted_metrics: dict[str, float]
    metric_name: str | None
    metric_direction: Literal["max", "min"] | None
    official_metric: float | None
    parent_record: RoundRecord | None
    baseline_metric: float | None
    comparison: MetricComparison | None
    delta_pct: float | None
    trusted: bool


def _candidate(data: RecordInput) -> CandidateEvidence:
    attempt = data.attempt
    response = attempt.implementation or attempt.single_agent_response
    if response is None:
        result = CandidateEvidence(CandidateDisposition.UNASSESSED.value, {}, None, "", "")
    else:
        result = CandidateEvidence(
            response.candidate_disposition.value,
            dict(response.candidate_metrics),
            response.candidate_evaluation_artifact,
            response.candidate_operating_point,
            response.candidate_retention_reason,
        )
    hypothesis = data.hypothesis
    if (
        result.disposition == CandidateDisposition.UNASSESSED.value
        and hypothesis.gate_revalidation_pending
        and hypothesis.gate_approved_candidate_disposition
        == CandidateDisposition.PARETO_FRONTIER.value
    ):
        return CandidateEvidence(
            hypothesis.gate_approved_candidate_disposition,
            dict(hypothesis.gate_approved_candidate_metrics),
            hypothesis.gate_approved_candidate_evaluation_artifact,
            hypothesis.gate_approved_candidate_operating_point,
            hypothesis.gate_approved_candidate_retention_reason,
        )
    return result


def _accepted_metrics(data: RecordInput) -> dict[str, float]:
    metrics = dict(data.projection.accepted_metrics)
    benchmark = data.attempt.framework_benchmark
    if data.projection.metric is not None and benchmark.row is not None:
        return dict(benchmark.row)
    if not metrics and data.projection.metric is not None and data.projection.unit is not None:
        return {data.projection.unit: data.projection.metric}
    return metrics


def _measurement(
    data: RecordInput, accepted_metrics: dict[str, float], *, official: bool
) -> MeasurementEvidence:
    projection = data.projection
    benchmark = data.attempt.framework_benchmark
    primary = data.state.metrics.primary
    metric_name = benchmark.metric_name or (primary.name if primary is not None else None)
    metric_name = metric_name or projection.unit
    direction = benchmark.metric_direction or (primary.direction if primary is not None else None)
    official_metric = (
        accepted_metrics.get(metric_name) if metric_name is not None else projection.metric
    )
    if official_metric is None and not accepted_metrics:
        official_metric = projection.metric
    parent = metric_baseline(
        parent_round=data.hypothesis.parent_round,
        parent_commit=data.hypothesis.parent_commit,
        metric=metric_name,
        rounds=data.records,
    )
    baseline = record_metric_value(parent, metric_name) if parent is not None else None
    trusted = trusted_perf_provenance(projection.provenance)
    reading = (
        Measurement(metric=metric_name, value=official_metric, direction=direction)
        if metric_name is not None and official_metric is not None
        else None
    )
    comparison = (
        data.state.metrics.compare(
            reading,
            Measurement(metric=metric_name, value=baseline, direction=direction)
            if metric_name is not None and baseline is not None
            else None,
        )
        if official and official_metric is not None and trusted
        else None
    )
    delta = (
        (official_metric - baseline) / abs(baseline) * 100
        if trusted and official_metric is not None and baseline not in {None, 0}
        else None
    )
    return MeasurementEvidence(
        accepted_metrics,
        metric_name,
        direction,
        official_metric,
        parent,
        baseline,
        comparison,
        delta,
        trusted,
    )


def _candidate_retained(
    data: RecordInput,
    candidate: CandidateEvidence,
    measurement: MeasurementEvidence,
    *,
    official: bool,
    reviewed: bool,
) -> bool | None:
    if not reviewed:
        return _provisional_candidate_retained(CandidateDisposition(candidate.disposition))
    if not data.attempt.passed:
        return False
    if (
        official
        and measurement.trusted
        and data.state.metrics.objectives
        and measurement.accepted_metrics
    ):
        return not _pareto_archive_dominators(
            measurement.accepted_metrics, data.records, data.state.metrics
        )
    if official and measurement.trusted:
        prior = [
            Measurement(
                metric=measurement.metric_name,
                value=value,
                direction=measurement.metric_direction,
            )
            for record in data.records
            if measurement.metric_name is not None
            and record.official_evaluation
            and trusted_perf_provenance(record.perf_provenance)
            and (value := record_metric_value(record, measurement.metric_name)) is not None
        ]
        reading = (
            Measurement(
                metric=measurement.metric_name,
                value=measurement.official_metric,
                direction=measurement.metric_direction,
            )
            if measurement.metric_name is not None and measurement.official_metric is not None
            else None
        )
        return scalar_candidate_retained(data.state.metrics.compare_to_best(reading, prior))
    return _provisional_candidate_retained(CandidateDisposition(candidate.disposition))


def build_round_record(data: RecordInput) -> RoundRecord:
    """Record final attempt, causal comparison, and trusted retention once."""
    attempt = data.attempt
    projection = data.projection
    candidate = _candidate(data)
    official = (
        attempt.passed
        and attempt.official_reason is not None
        and data.backend_name != "stub"
        and (data.accuracy_configured or data.framework_benchmark_configured)
    )
    reviewed = data.reviewed
    metrics = _measurement(data, _accepted_metrics(data), official=official)
    declared = (
        attempt.implementation.hypothesis_outcome
        if attempt.implementation is not None
        else HypothesisOutcome.NOMINATED
        if attempt.single_agent_response is not None
        else None
    )
    resolution = resolve_hypothesis_outcome(
        ResolutionEvidence(
            declared=declared,
            passed=attempt.passed,
            reviewed=reviewed,
            comparison=metrics.comparison,
        )
    )
    return RoundRecord(
        round_number=data.round_number,
        commit=data.candidate_commit,
        perf_metric=projection.metric,
        perf_unit=projection.unit,
        passed=attempt.passed,
        profile_skipped=projection.profile_skipped,
        hypothesis_id=data.plan.hypothesis_id,
        hypothesis_declared_outcome=declared.value if declared is not None else None,
        judge_verdict=recorded_judge_verdict(attempt.judge),
        hypothesis_outcome=(
            resolution.value if resolution is not None else declared.value if declared else None
        ),
        hypothesis_claim=data.plan.hypothesis or None,
        hypothesis_task=data.plan.task or None,
        hypothesis_parent_round=data.hypothesis.parent_round,
        hypothesis_parent_commit=data.hypothesis.parent_commit,
        metrics=metrics.accepted_metrics,
        evaluation_artifact=projection.accepted_evaluation_artifact,
        official_evaluation=official,
        official_evaluation_reason=attempt.official_reason if official else None,
        candidate_disposition=candidate.disposition,
        candidate_metrics=candidate.metrics,
        candidate_evaluation_artifact=candidate.artifact,
        candidate_operating_point=candidate.operating_point,
        candidate_retention_reason=candidate.retention_reason,
        candidate_retained=_candidate_retained(
            data, candidate, metrics, official=official, reviewed=reviewed
        ),
        perf_direction=metrics.metric_direction,
        perf_baseline_round=(
            metrics.parent_record.round_number if metrics.parent_record is not None else None
        ),
        perf_baseline_commit=(
            metrics.parent_record.commit if metrics.parent_record is not None else None
        ),
        perf_baseline_metric=metrics.baseline_metric,
        perf_delta_pct=metrics.delta_pct,
        perf_comparison=metrics.comparison,
        perf_provenance=projection.provenance,
        implementer_driver=data.driver_name,
        implementer_provider=data.provider,
        implementer_model=data.model,
    )
