"""Pure state transitions for the hypothesis search.

Ported from ``vibesys.agent_run.hypotheses`` and ``vibesys.agent_run.evidence``
(copied, not imported: those modules are deleted once the strategies are
rewired onto this package). Semantics are unchanged; only names and module
boundaries were cleaned up. Every function here is a pure function of its
arguments: no ``RunContext``, no agents, no prompts, no filesystem, no clock,
no global RNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from vibesys.evaluators.metrics import Measurement, MetricComparison, MetricSpace
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisStrategyUpdate,
    OrchestratorPlan,
    PerfDeltaReason,
)
from vibesys.search.hypothesis.state import (
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisState,
    HypothesisStrategy,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_loop_state.api import PerfProvenance, RoundRecord

# ``hypothesis_outcome`` values that mark a hypothesis campaign as failed, for
# rollback-target resolution. A record with no outcome yet is never "failed".
FAILED_HYPOTHESIS_OUTCOMES = (
    frozenset(outcome.value for outcome in HypothesisOutcome)
    - {
        HypothesisOutcome.CONTINUE.value,
        HypothesisOutcome.SUPPORTED.value,
        HypothesisOutcome.NOMINATED.value,
    }
) | {"rejected"}

_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT = 8
_PLATEAU_THRESHOLD_PCT = 5.0
_PLATEAU_MIN_STREAK = 3


def trusted_perf_provenance(provenance: PerfProvenance | None) -> bool:
    """Whether a round's headline metric may drive a framework decision.

    The one trust rule shared by hypothesis resolution, scalar and Pareto
    retention, the recorded delta, and trusted Pareto-parent selection.
    Legacy records carry no provenance and stay trusted, so reprojecting an
    old run does not rewrite its historical resolutions; only an explicit
    agent self-report is untrusted.
    """
    return provenance != "implementer"


@dataclass(frozen=True)
class ResolutionEvidence:
    """Inputs needed to finalize one hypothesis declaration.

    ``comparison`` is the round's headline reading ordered against its causal
    baseline, set only for a reading the framework measured itself. ``None``
    covers both "no official metric was recorded" and "the number on the
    record is the implementer's own report".
    """

    declared: HypothesisOutcome | None
    passed: bool
    reviewed: bool
    comparison: MetricComparison | None


def resolve_hypothesis_outcome(evidence: ResolutionEvidence) -> HypothesisResolution | None:
    """Resolve one declaration only after review and trusted evidence.

    A supportive declaration (``SUPPORTED``/``NOMINATED``) never resolves
    ``PROVEN`` on the agent's word alone: without a trusted measurement it
    resolves ``UNMEASURED``, and with one the measurement decides.
    """
    if not evidence.reviewed:
        resolution = None
    elif not evidence.passed:
        resolution = HypothesisResolution.REJECTED
    elif evidence.declared is None:
        resolution = None
    else:
        resolution = {
            HypothesisOutcome.DISPROVEN: HypothesisResolution.DISPROVEN,
            HypothesisOutcome.IMPLEMENTATION_FAILED: HypothesisResolution.IMPLEMENTATION_FAILED,
            HypothesisOutcome.INCONCLUSIVE: HypothesisResolution.INCONCLUSIVE,
            HypothesisOutcome.BLOCKED: HypothesisResolution.BLOCKED,
        }.get(evidence.declared)
        if evidence.declared is HypothesisOutcome.CONTINUE:
            resolution = None
        elif resolution is None:
            resolution = (
                HypothesisResolution.UNMEASURED
                if evidence.comparison is None
                else _resolve_metric_evidence(evidence.comparison)
            )
    return resolution


def _resolve_metric_evidence(comparison: MetricComparison) -> HypothesisResolution:
    match comparison:
        case MetricComparison.BETTER:
            return HypothesisResolution.PROVEN
        case MetricComparison.WORSE:
            return HypothesisResolution.DISPROVEN
        case MetricComparison.WITHIN_NOISE | MetricComparison.INCOMPARABLE:
            return HypothesisResolution.INCONCLUSIVE
    assert_never(comparison)


def scalar_candidate_retained(comparison: MetricComparison) -> bool | None:
    """Return whether an official scalar candidate advances the best checkpoint.

    *comparison* orders the candidate against the best prior official
    reading, from ``MetricSpace.compare_to_best``. An empty history compares
    as ``BETTER``, so the first trusted checkpoint is retained.
    """
    match comparison:
        case MetricComparison.BETTER:
            return True
        case MetricComparison.WORSE | MetricComparison.WITHIN_NOISE:
            return False
        case MetricComparison.INCOMPARABLE:
            return None


def record_metric_value(record: RoundRecord, metric: str | None) -> float | None:
    """Read one official metric off *record* without guessing across axes."""
    if metric is not None and metric in record.metrics:
        return record.metrics[metric]
    if record.perf_metric is not None and (
        metric is None or record.perf_unit == metric or not record.metrics
    ):
        return record.perf_metric
    return None


def metric_baseline(
    *,
    parent_round: int | None,
    parent_commit: str | None,
    metric: str | None,
    rounds: Sequence[RoundRecord],
) -> RoundRecord | None:
    """Find the baseline for one metric, preferring the exact causal parent."""
    comparable = baseline_candidates(metric=metric, rounds=rounds)
    if not comparable:
        return None
    if parent_commit is not None:
        exact = next(
            (item for item in reversed(comparable) if item.commit == parent_commit),
            None,
        )
        if exact is not None:
            return exact
    if parent_round is not None:
        return next(
            (item for item in reversed(comparable) if item.round_number <= parent_round),
            None,
        )
    if parent_commit is not None:
        # A causal parent was named but no trusted official round carries
        # that commit, and there is no round number to bound the fallback
        # by. Fail closed rather than let a later round serve as baseline.
        return None
    return comparable[-1]


def baseline_candidates(
    *,
    metric: str | None,
    rounds: Sequence[RoundRecord],
) -> list[RoundRecord]:
    """Return the rounds admissible as a baseline for *metric*, in order."""
    return [
        item
        for item in rounds
        if item.official_evaluation
        and item.perf_metric is not None
        and trusted_perf_provenance(item.perf_provenance)
        and (item.perf_unit == metric or (metric is not None and metric in item.metrics))
    ]


def measurement_delta_reason(hypothesis: Hypothesis) -> PerfDeltaReason | None:
    """Why *hypothesis*'s headline number carries no causal delta."""
    if hypothesis.measurement is not None:
        return hypothesis.measurement.delta_reason
    if any(
        record.official_evaluation
        and record.perf_metric is not None
        and not trusted_perf_provenance(record.perf_provenance)
        for record in hypothesis.rounds
    ):
        return PerfDeltaReason.NOT_FRAMEWORK_MEASURED
    return None


def start_hypothesis(
    state: HypothesisState,
    plan: OrchestratorPlan,
    *,
    started_round: int,
    parent_round: int | None = None,
    parent_commit: str | None = None,
) -> HypothesisState:
    """Start a new hypothesis after applying its strategic updates."""
    if state.active_hypothesis_id is not None:
        raise ValueError("cannot start a hypothesis while another is active")  # noqa: TRY003
    identifier = plan.hypothesis_id.strip()
    if not identifier:
        raise ValueError("hypothesis ID must not be blank")  # noqa: TRY003
    if state.by_id(identifier) is not None:
        raise ValueError(f"hypothesis ID {identifier!r} already exists")  # noqa: TRY003
    updated = apply_strategy_updates(state, plan.hypothesis_updates)
    updated.hypotheses.append(
        Hypothesis(
            hypothesis_id=identifier,
            plan=plan.model_copy(update={"hypothesis_id": identifier}, deep=True),
            started_round=started_round,
            parent_round=parent_round,
            parent_commit=parent_commit,
        )
    )
    updated.active_hypothesis_id = identifier
    _advance_experiment_revision(
        updated,
        {identifier, *(change.hypothesis_id for change in plan.hypothesis_updates)},
    )
    return _validated_state(updated)


def update_active_hypothesis(state: HypothesisState, hypothesis: Hypothesis) -> HypothesisState:
    """Replace the active hypothesis with an updated restart checkpoint."""
    identifier = state.active_hypothesis_id
    if identifier is None:
        raise ValueError("cannot update an active hypothesis when none is active")  # noqa: TRY003
    if hypothesis.hypothesis_id != identifier:
        raise ValueError("updated hypothesis must preserve the active hypothesis ID")  # noqa: TRY003
    updated = state.clone()
    index = next(
        index for index, item in enumerate(updated.hypotheses) if item.hypothesis_id == identifier
    )
    updated.hypotheses[index] = hypothesis.model_copy(deep=True)
    return _validated_state(updated)


def append_round(
    state: HypothesisState,
    record: RoundRecord,
    *,
    keep_active: bool,
) -> HypothesisState:
    """Append one completed round to the active hypothesis."""
    active = state.active_hypothesis
    if active is None:
        raise ValueError("cannot append a round when no hypothesis is active")  # noqa: TRY003
    if record.hypothesis_id != active.hypothesis_id:
        raise ValueError("round hypothesis_id must match the active hypothesis")  # noqa: TRY003
    if any(item.round_number == record.round_number for item in state.rounds):
        raise ValueError(f"round {record.round_number} already exists")  # noqa: TRY003

    updated = state.clone()
    updated_active = updated.active_hypothesis
    assert updated_active is not None  # noqa: S101  # preserved by the clone
    projected = project_round_evidence(
        updated_active,
        record,
        prior_rounds=state.rounds,
        space=state.metrics,
    )
    index = next(
        index
        for index, item in enumerate(updated.hypotheses)
        if item.hypothesis_id == projected.hypothesis_id
    )
    updated.hypotheses[index] = projected
    if not keep_active:
        updated.active_hypothesis_id = None
    _advance_experiment_revision(updated, {projected.hypothesis_id})
    return _validated_state(updated)


def finish_hypothesis(state: HypothesisState) -> HypothesisState:
    """Clear the active pointer without changing the hypothesis itself."""
    if state.active_hypothesis_id is None:
        return state.clone()
    updated = state.clone()
    active_id = updated.active_hypothesis_id
    updated.active_hypothesis_id = None
    assert active_id is not None  # noqa: S101  # checked above
    _advance_experiment_revision(updated, {active_id})
    return _validated_state(updated)


def apply_strategy_updates(
    state: HypothesisState,
    updates: Sequence[HypothesisStrategyUpdate],
) -> HypothesisState:
    """Apply orchestrator-owned parked/abandoned decisions."""
    updated = state.clone()
    seen: set[str] = set()
    for change in updates:
        if change.hypothesis_id in seen:
            raise ValueError(  # noqa: TRY003
                f"duplicate strategy update for hypothesis {change.hypothesis_id!r}"
            )
        seen.add(change.hypothesis_id)
        index = next(
            (
                index
                for index, item in enumerate(updated.hypotheses)
                if item.hypothesis_id == change.hypothesis_id
            ),
            None,
        )
        if index is None:
            raise ValueError(  # noqa: TRY003
                f"strategy update names unknown hypothesis {change.hypothesis_id!r}"
            )
        item = updated.hypotheses[index]
        if updated.active_hypothesis_id == change.hypothesis_id:
            raise ValueError(  # noqa: TRY003
                f"cannot {change.disposition} active hypothesis {change.hypothesis_id!r}"
            )
        if not item.rounds:
            raise ValueError(  # noqa: TRY003
                f"cannot update incomplete hypothesis {change.hypothesis_id!r}"
            )
        item.strategy = HypothesisStrategy(change.disposition)
        item.strategy_reason = change.reason.strip()
    return _validated_state(updated)


def project_round_evidence(
    hypothesis: Hypothesis,
    record: RoundRecord,
    *,
    prior_rounds: Sequence[RoundRecord],
    space: MetricSpace,
) -> Hypothesis:
    """Return a hypothesis updated with one completed round's evidence."""
    if record.hypothesis_id != hypothesis.hypothesis_id:
        raise ValueError("round hypothesis_id must match its owning hypothesis")  # noqa: TRY003
    if any(item.round_number == record.round_number for item in hypothesis.rounds):
        raise ValueError(f"round {record.round_number} already belongs to hypothesis")  # noqa: TRY003
    updated = hypothesis.clone()
    updated.rounds.append(record)
    updated.declared_outcome = _declared_outcome(record.hypothesis_declared_outcome)
    updated.review = _review(record)
    measurement = _measurement(record, prior_rounds, space)
    comparison = round_comparison(record, measurement, space)
    if record.judge_verdict is not None:
        updated.resolution = resolve_hypothesis_outcome(
            ResolutionEvidence(
                declared=updated.declared_outcome,
                passed=record.passed,
                reviewed=updated.review
                not in {HypothesisReview.PENDING, HypothesisReview.DEFERRED},
                comparison=comparison,
            )
        )
    else:
        updated.resolution = (
            _resolution(record.hypothesis_outcome)
            if updated.review not in {HypothesisReview.PENDING, HypothesisReview.DEFERRED}
            else None
        )
    if measurement is not None:
        updated.measurement = measurement
    retained = _retained(record, prior_rounds, space)
    if retained is not None:
        updated.candidate_retained = retained
    if record.judge_verdict is None:
        _correct_legacy_resolution(updated, record, measurement, comparison)
    return Hypothesis.model_validate(updated.model_dump())


def round_comparison(
    record: RoundRecord,
    measurement: HypothesisMeasurement | None,
    space: MetricSpace,
) -> MetricComparison | None:
    """Return how one round's headline reading compared with its baseline.

    A record that carries ``perf_comparison`` answers for itself, so a
    resumed run does not disagree with what it recorded. The provenance guard
    must come first: a self-reported round stores no comparison, which is
    indistinguishable from a pre-provenance record, and without the guard the
    fallback would re-derive one and resolve the hypothesis differently than
    the loop that wrote it did.
    """
    if not record.official_evaluation or record.perf_metric is None:
        return None
    if not trusted_perf_provenance(record.perf_provenance):
        return None
    if record.perf_comparison is not None:
        return record.perf_comparison
    if measurement is None:
        return MetricComparison.INCOMPARABLE
    return space.compare(
        _reading(measurement, measurement.value),
        _reading(measurement, measurement.baseline_value),
    )


def _reading(measurement: HypothesisMeasurement, value: float | None) -> Measurement | None:
    if value is None:
        return None
    return Measurement(metric=measurement.metric, value=value, direction=measurement.direction)


def adopt_metric_space(state: HypothesisState, space: MetricSpace) -> HypothesisState:
    """Record the run's metric space and re-derive evidence within it."""
    updated = state.clone()
    updated.metrics = space
    reprojected = reproject_run_evidence(updated)
    changed = {
        current.hypothesis_id
        for current, previous in zip(reprojected.hypotheses, state.hypotheses, strict=True)
        if current != previous
    }
    if changed:
        _advance_experiment_revision(reprojected, changed)
    return _validated_state(reprojected)


def _advance_experiment_revision(state: HypothesisState, hypothesis_ids: set[str]) -> None:
    state.experiment_revision += 1
    for hypothesis in state.hypotheses:
        if hypothesis.hypothesis_id in hypothesis_ids:
            hypothesis.last_experiment_revision = state.experiment_revision


def reproject_run_evidence(state: HypothesisState) -> HypothesisState:
    """Rebuild hypothesis summaries from their authoritative round evidence."""
    updated = state.clone()
    updated.hypotheses = [
        hypothesis.model_copy(
            update={
                "rounds": [],
                "declared_outcome": None,
                "review": HypothesisReview.PENDING,
                "resolution": None,
                "measurement": None,
                "candidate_retained": None,
            },
            deep=True,
        )
        for hypothesis in updated.hypotheses
    ]
    prior_rounds: list[RoundRecord] = []
    for record in state.rounds:
        index = next(
            (
                index
                for index, hypothesis in enumerate(updated.hypotheses)
                if hypothesis.hypothesis_id == record.hypothesis_id
            ),
            None,
        )
        if index is None:
            raise ValueError(  # noqa: TRY003
                f"round {record.round_number} names unknown hypothesis {record.hypothesis_id!r}"
            )
        updated.hypotheses[index] = project_round_evidence(
            updated.hypotheses[index],
            record,
            prior_rounds=prior_rounds,
            space=state.metrics,
        )
        prior_rounds.append(record)
    return _validated_state(updated)


def _validated_state(state: HypothesisState) -> HypothesisState:
    return HypothesisState.model_validate(state.model_dump())


def _correct_legacy_resolution(
    hypothesis: Hypothesis,
    record: RoundRecord,
    measurement: HypothesisMeasurement | None,
    comparison: MetricComparison | None,
) -> None:
    """Re-decide a legacy record's self-declared ``proven`` from its evidence."""
    if record.judge_verdict is not None or hypothesis.resolution is not HypothesisResolution.PROVEN:
        return
    if (
        record.official_evaluation
        and record.perf_metric is not None
        and (measurement is None or measurement.direction is None or measurement.delta_pct is None)
    ):
        hypothesis.resolution = HypothesisResolution.INCONCLUSIVE
        return
    if measurement is None or comparison is None:
        return
    match comparison:
        case MetricComparison.BETTER:
            pass
        case MetricComparison.WORSE:
            hypothesis.resolution = HypothesisResolution.DISPROVEN
        case MetricComparison.WITHIN_NOISE | MetricComparison.INCOMPARABLE:
            hypothesis.resolution = HypothesisResolution.INCONCLUSIVE


def _declared_outcome(value: str | None) -> HypothesisOutcome | None:
    if value is None:
        return None
    try:
        return HypothesisOutcome(value)
    except ValueError:
        return None


def _review(record: RoundRecord) -> HypothesisReview:
    if record.judge_verdict is not None:
        return HypothesisReview(record.judge_verdict)
    if not record.reviewed:
        return HypothesisReview.DEFERRED
    return HypothesisReview.PASS if record.passed else HypothesisReview.FAIL


def _resolution(value: str | None) -> HypothesisResolution | None:
    if value is None or value == HypothesisOutcome.CONTINUE.value:
        return None
    if value in {HypothesisOutcome.SUPPORTED.value, HypothesisOutcome.NOMINATED.value}:
        return None
    try:
        return HypothesisResolution(value)
    except ValueError:
        return HypothesisResolution.INCONCLUSIVE


def _measurement(
    record: RoundRecord,
    prior_rounds: Sequence[RoundRecord],
    space: MetricSpace,
) -> HypothesisMeasurement | None:
    if (
        not record.official_evaluation
        or record.perf_metric is None
        or record.perf_unit is None
        or not trusted_perf_provenance(record.perf_provenance)
    ):
        return None
    direction = space.direction(
        Measurement(
            metric=record.perf_unit, value=record.perf_metric, direction=record.perf_direction
        )
    )
    baseline = _baseline(record, prior_rounds)
    baseline_round = (
        record.perf_baseline_round
        if record.perf_baseline_round is not None
        else baseline.round_number
        if baseline is not None
        else None
    )
    baseline_commit = record.perf_baseline_commit or (
        baseline.commit if baseline is not None else None
    )
    baseline_value = record.perf_baseline_metric
    if baseline_value is None and baseline is not None:
        baseline_value = record_metric_value(baseline, record.perf_unit)
    delta = record.perf_delta_pct
    if delta is None and baseline_value not in {None, 0}:
        assert baseline_value is not None  # noqa: S101  # narrowed above
        delta = (record.perf_metric - baseline_value) / abs(baseline_value) * 100
    delta_reason = None
    if (
        delta is None
        and baseline_round is None
        and baseline_commit is None
        and baseline_value is None
        and record.perf_provenance is not None
    ):
        delta_reason = (
            PerfDeltaReason.BASELINE_UNRESOLVED
            if baseline_candidates(metric=record.perf_unit, rounds=prior_rounds)
            else PerfDeltaReason.NO_BASELINE_YET
        )
    return HypothesisMeasurement(
        round=record.round_number,
        metric=record.perf_unit,
        value=record.perf_metric,
        unit=record.perf_unit,
        direction=direction,
        baseline_round=baseline_round,
        baseline_commit=baseline_commit,
        baseline_value=baseline_value,
        delta_pct=delta,
        delta_reason=delta_reason,
    )


def _baseline(record: RoundRecord, prior_rounds: Sequence[RoundRecord]) -> RoundRecord | None:
    return metric_baseline(
        parent_round=record.hypothesis_parent_round,
        parent_commit=record.hypothesis_parent_commit,
        metric=record.perf_unit,
        rounds=prior_rounds,
    )


def _retained(
    record: RoundRecord,
    prior_rounds: Sequence[RoundRecord],
    space: MetricSpace,
) -> bool | None:
    if record.candidate_retained is not None:
        retained = record.candidate_retained
    elif record.judge_verdict is not None:
        retained = None
    elif record.candidate_disposition in {"pareto_frontier", "prerequisite"}:
        retained = True
    elif record.candidate_disposition == "discard":
        retained = False
    elif not record.official_evaluation or record.perf_metric is None or record.perf_unit is None:
        retained = None
    else:
        candidate = Measurement(
            metric=record.perf_unit, value=record.perf_metric, direction=record.perf_direction
        )
        axis = space.direction(candidate)
        comparable = [
            Measurement(metric=record.perf_unit, value=value, direction=axis)
            for prior in prior_rounds
            if prior.official_evaluation
            and prior.passed
            and trusted_perf_provenance(prior.perf_provenance)
            and (value := record_metric_value(prior, record.perf_unit)) is not None
        ]
        retained = scalar_candidate_retained(space.compare_to_best(candidate, comparable))
    return retained


# --- Evidence: retention, frontier, and carry-over text (from agent_run.evidence) ---


def record_candidate_metrics(record: RoundRecord) -> dict[str, float]:
    """Return the comparable objective row associated with *record*."""
    if record.official_evaluation and record.metrics:
        return record.metrics
    if record_candidate_retained(record) is True:
        return record.candidate_metrics
    return {}


def record_candidate_retained(record: RoundRecord) -> bool | None:
    """Read framework retention, with one isolated legacy-record adapter."""
    if record.candidate_retained is not None:
        return record.candidate_retained
    if record.judge_verdict is not None:
        return None
    if record.candidate_disposition in {
        CandidateDisposition.PARETO_FRONTIER.value,
        CandidateDisposition.PREREQUISITE.value,
    }:
        return True
    if record.candidate_disposition == CandidateDisposition.DISCARD.value:
        return False
    if record.hypothesis_outcome == HypothesisResolution.PROVEN.value:
        return True
    return None


def provisional_candidate_retained(disposition: CandidateDisposition) -> bool | None:
    """Translate an implementer disposition into provisional branch retention."""
    if disposition is CandidateDisposition.DISCARD:
        return False
    if disposition in {CandidateDisposition.PREREQUISITE, CandidateDisposition.PARETO_FRONTIER}:
        return True
    return None


def trusted_candidate_records(
    records: Sequence[RoundRecord], space: MetricSpace
) -> list[RoundRecord]:
    """Return reviewed checkpoints with complete comparable objective rows."""
    trusted: list[RoundRecord] = []
    for record in records:
        if not space.complete(record_candidate_metrics(record)):
            continue
        if not record.commit or not record.passed or not record.reviewed:
            continue
        if not trusted_perf_provenance(record.perf_provenance):
            continue
        if record_candidate_retained(record) is not True:
            continue
        trusted.append(record)
    return trusted


def pareto_frontier_records(
    records: Sequence[RoundRecord], space: MetricSpace
) -> list[RoundRecord]:
    """Compute the noise-aware frontier over independently reviewed points."""
    return space.frontier(trusted_candidate_records(records, space), record_candidate_metrics)


def pareto_archive_dominators(
    candidate_metrics: dict[str, float],
    records: Sequence[RoundRecord],
    space: MetricSpace,
) -> list[RoundRecord]:
    """Return trusted archive points that dominate a proposed candidate row."""
    if not space.complete(candidate_metrics):
        return []
    return [
        record
        for record in trusted_candidate_records(records, space)
        if space.dominates(record_candidate_metrics(record), candidate_metrics)
    ]


def _format_metric_row(metrics: dict[str, float], objectives: Sequence) -> str:
    return ", ".join(
        f"{objective.name}={metrics[objective.name]:.6g} ({objective.direction})"
        for objective in objectives
        if objective.name in metrics
    )


def pareto_archive_conflict(
    *,
    candidate_disposition: CandidateDisposition,
    candidate_metrics: dict[str, float],
    records: Sequence[RoundRecord],
    space: MetricSpace,
) -> str | None:
    """Explain why a claimed frontier row is dominated by the live archive."""
    if candidate_disposition is not CandidateDisposition.PARETO_FRONTIER:
        return None
    dominators = pareto_archive_dominators(candidate_metrics, records, space)
    if not dominators:
        return None
    rows = "; ".join(
        f"round {record.round_number} "
        f"({_format_metric_row(record_candidate_metrics(record), space.objectives)})"
        for record in dominators
    )
    return (
        "The candidate's `pareto_frontier` disposition conflicts with the live "
        f"noise-aware archive: it is dominated by {rows}. A numeric archive gate "
        "frozen into the hypothesis plan does not override the current archive. "
        "Report this row as `discard` unless its metrics or configured objective "
        "comparability were recorded incorrectly; do not rerun an unchanged "
        "candidate merely to repair the disposition."
    )


def headline_measurement(record: RoundRecord) -> Measurement | None:
    """Return a record's scalar headline as a typed measurement."""
    if record.perf_metric is None or record.perf_unit is None:
        return None
    return Measurement(
        metric=record.perf_unit, value=record.perf_metric, direction=record.perf_direction
    )


def trusted_final_records(records: Sequence[RoundRecord], space: MetricSpace) -> list[RoundRecord]:
    """Return retained records with canonical framework-owned measurements."""
    return [
        record
        for record in records
        if record.commit
        and record.passed
        and record.reviewed
        and record.official_evaluation
        and trusted_perf_provenance(record.perf_provenance)
        and record_candidate_retained(record) is True
        and (
            space.complete(record.metrics)
            if space.objectives
            else space.direction(headline_measurement(record)) is not None
        )
    ]


def select_final_candidate(
    records: Sequence[RoundRecord], space: MetricSpace
) -> RoundRecord | None:
    """Select the latest noise-aware winner from trusted retained records."""
    newest_first = sorted(
        trusted_final_records(records, space), key=lambda record: record.round_number, reverse=True
    )
    if space.primary is not None:
        frontier_rounds = {
            record.round_number for record in pareto_frontier_records(newest_first, space)
        }
        candidates = [record for record in newest_first if record.round_number in frontier_rounds]
        primary = space.primary

        def primary_measurement(record: RoundRecord) -> Measurement:
            return Measurement(
                metric=primary.name, value=record.metrics[primary.name], direction=primary.direction
            )

        return space.best(candidates, primary_measurement)
    return space.best(newest_first, headline_measurement)


def detect_plateau(
    records: Sequence[RoundRecord],
    *,
    threshold_pct: float = _PLATEAU_THRESHOLD_PCT,
    min_streak: int = _PLATEAU_MIN_STREAK,
) -> str | None:
    """Warn when the most recent fresh, same-unit official readings plateaued.

    Only rounds the framework measured itself count (an implementer's
    self-report is not evidence the search has stopped progressing), and only
    rounds sharing the latest fresh round's unit count toward the streak.
    """
    fresh = [
        r
        for r in records
        if r.passed
        and r.official_evaluation
        and r.perf_metric is not None
        and trusted_perf_provenance(r.perf_provenance)
        and not r.profile_skipped
    ]
    if len(fresh) < min_streak:
        return None
    latest_unit = fresh[-1].perf_unit
    same_unit = [r for r in fresh if r.perf_unit == latest_unit]
    if len(same_unit) < min_streak:
        return None
    tail = same_unit[-min_streak:]
    perfs = [r.perf_metric for r in tail if r.perf_metric is not None]
    hi = max(perfs)
    lo = min(perfs)
    if hi <= 0:
        return None
    spread_pct = (hi - lo) / hi * 100
    if spread_pct >= threshold_pct:
        return None
    unit_suffix = f" {latest_unit}" if latest_unit else ""
    rounds = [r.round_number for r in tail]
    return (
        f"The last {min_streak} rounds with a fresh perf measurement (rounds "
        f"{rounds[0]}–{rounds[-1]}) all landed in {lo:.2f}–{hi:.2f}{unit_suffix} "  # noqa: RUF001
        f"— a {spread_pct:.2f}% spread, well within bench noise. Whatever you've "
        f"been working on for those rounds is not actually moving the headline metric."
    )


@dataclass
class CarryOver:
    """Record-derived guidance passed to the next planning turn."""

    regression_info: str | None = None
    exhaustion_info: str | None = None


def provisional_candidates_since_official(records: Sequence[RoundRecord]) -> int:
    """Count accepted candidate checkpoints after the latest official one."""
    count = 0
    for record in reversed(records):
        if record.official_evaluation:
            break
        if (
            record.passed
            and record.reviewed
            and (
                record_candidate_retained(record) is True
                or record.hypothesis_outcome
                in {HypothesisResolution.PROVEN.value, HypothesisResolution.UNMEASURED.value}
            )
        ):
            count += 1
    return count


def terminal_workspace_notice(records: Sequence[RoundRecord]) -> str | None:
    """Describe a terminal hypothesis whose edits remain in the workspace."""
    if not records:
        return None
    latest = records[-1]
    terminal_outcomes = {
        HypothesisOutcome.DISPROVEN.value,
        HypothesisOutcome.IMPLEMENTATION_FAILED.value,
        HypothesisOutcome.INCONCLUSIVE.value,
        HypothesisOutcome.BLOCKED.value,
    }
    if latest.hypothesis_outcome not in terminal_outcomes:
        return None

    if record_candidate_retained(latest) is True:
        review_status = (
            "independently reviewed"
            if latest.passed and latest.reviewed
            else "awaiting independent review"
        )
        return (
            f"Hypothesis `{latest.hypothesis_id or 'unspecified'}` ended as "
            f"`{latest.hypothesis_outcome}` in round {latest.round_number}, but its "
            f"implementation reported a {review_status} Pareto checkpoint: "
            f"{latest.candidate_metrics or '(metrics missing)'}. Preserve commit "
            f"`{(latest.commit or '(missing)')[:12]}` as a distinct branch candidate. "
            "The causal forecast and checkpoint retention decision are separate: do "
            "not erase a credible throughput/latency tradeoff merely because another "
            "axis or the forecast missed. If review is pending, validate hard "
            "correctness and workload invariants before using it as a trusted parent. "
            "Choose this checkpoint only when the next hypothesis names which frontier "
            "gap it will improve; otherwise explicitly restore another frontier parent."
        )

    campaign_records = [latest]
    for record in reversed(records[:-1]):
        if record.hypothesis_id != latest.hypothesis_id:
            break
        campaign_records.append(record)
    campaign_records.reverse()
    started_round = campaign_records[0].round_number
    parent_round = next(
        (
            record.hypothesis_parent_round
            for record in campaign_records
            if record.hypothesis_parent_round is not None
        ),
        started_round - 1 if started_round > 1 else None,
    )
    parent_guidance = (
        f"The recorded pre-hypothesis parent is round {parent_round}; use "
        f"`revert_to_round={parent_round}` if that parent should be restored."
        if parent_round is not None
        else "No earlier recorded round exists, so identify the clean parent state explicitly."
    )
    latest_checkpoint = next(
        (
            record
            for record in reversed(records[:-1])
            if record.commit is not None
            and record.hypothesis_outcome in {HypothesisOutcome.CONTINUE.value, "proven"}
        ),
        None,
    )
    checkpoint_guidance = ""
    if latest_checkpoint is not None and latest_checkpoint.round_number != parent_round:
        review_label = "reviewed" if latest_checkpoint.reviewed else "provisional"
        checkpoint_guidance = (
            " The most recent earlier nonterminal checkpoint is round "
            f"{latest_checkpoint.round_number} "
            f"(`{latest_checkpoint.hypothesis_outcome}`, {review_label}). If the "
            "terminal evidence rejects only the newest child experiment, preserve "
            "that checkpoint instead of discarding prior gains; restore the original "
            "pre-hypothesis parent only when the evidence invalidates the full chain. "
            f"If metrics from round {latest_checkpoint.round_number} are the "
            "restoration gate, restore that checkpoint or preserve all production "
            "changes through it. An older implementation cannot be required to "
            "reproduce a later checkpoint's metric while those later gains are omitted."
        )
    return (
        f"Hypothesis `{latest.hypothesis_id or 'unspecified'}` ended as "
        f"`{latest.hypothesis_outcome}` in round {latest.round_number}, but its "
        "workspace edits are still present. Before building a new hypothesis, "
        "decide explicitly whether to roll those edits back or retain a reusable "
        "correctness/measurement prerequisite. Do not silently build on a "
        f"falsified performance mechanism. {parent_guidance}{checkpoint_guidance} "
        "If retaining any part, justify it and re-establish the end-to-end parent behavior."
    )
