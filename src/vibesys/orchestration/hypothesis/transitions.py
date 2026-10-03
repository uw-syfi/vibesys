"""Pure state transitions for the hypothesis search.

Ported from the former ``vibesys.agent_run.hypotheses`` and
``vibesys.agent_run.evidence`` modules (agent_run has since dissolved).
Semantics are unchanged; only names and module boundaries were cleaned up.
Every function here is a pure function of its arguments: no runtime capabilities,
no agents, no prompts, no filesystem, no clock, no global RNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from vibesys.orchestration.hypothesis.notices import (
    ArchiveAxis,
    ArchiveConflict,
    ArchiveDominator,
    ArchiveLatestRound,
    ArchiveMetric,
    ArchivePendingClaim,
    ArchiveScalarReading,
    ArchiveTrustedParent,
    ExhaustionNotice,
    OmittedPendingClaims,
    ParetoArchiveView,
    RegressionNotice,
    RetainedTerminalCheckpoint,
    TerminalWorkspaceEdits,
    WorkspaceCheckpoint,
)
from vibesys.orchestration.hypothesis.state import (
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisState,
    HypothesisStrategy,
)
from vibesys.orchestration.metrics import Measurement, MetricComparison, MetricSpace
from vs_loop_state.api import (
    CandidateDisposition,
    HypothesisOutcome,
    PerfDeltaReason,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.orchestration.hypothesis.plan import HypothesisStrategyUpdate, OrchestratorPlan
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
_EN_DASH = "\N{EN DASH}"


def trusted_perf_provenance(provenance: PerfProvenance | None) -> bool:
    """Whether a round's headline metric may drive a framework decision.

    The one trust rule shared by hypothesis resolution, scalar and Pareto
    retention, the recorded delta, and trusted Pareto-parent selection.
    Only an explicit framework measurement is trusted. Missing provenance
    fails closed.
    """
    return provenance == "framework"


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
        message = "cannot start a hypothesis while another is active"
        raise ValueError(message)
    identifier = plan.hypothesis_id.strip()
    if not identifier:
        _exception_message = "hypothesis ID must not be blank"
        raise ValueError(_exception_message)
    if state.by_id(identifier) is not None:
        _exception_message_2 = f"hypothesis ID {identifier!r} already exists"
        raise ValueError(_exception_message_2)
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
        message = "cannot update an active hypothesis when none is active"
        raise ValueError(message)
    if hypothesis.hypothesis_id != identifier:
        _exception_message = "updated hypothesis must preserve the active hypothesis ID"
        raise ValueError(_exception_message)
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
        message = "cannot append a round when no hypothesis is active"
        raise ValueError(message)
    if record.hypothesis_id != active.hypothesis_id:
        _exception_message = "round hypothesis_id must match the active hypothesis"
        raise ValueError(_exception_message)
    if any(item.round_number == record.round_number for item in state.rounds):
        _exception_message_2 = f"round {record.round_number} already exists"
        raise ValueError(_exception_message_2)

    updated = state.clone()
    projected = project_round_evidence(
        active,
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
    active_id = state.active_hypothesis_id
    if active_id is None:
        return state.clone()
    updated = state.clone()
    updated.active_hypothesis_id = None
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
            message = f"duplicate strategy update for hypothesis {change.hypothesis_id!r}"
            raise ValueError(message)
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
            message = f"strategy update names unknown hypothesis {change.hypothesis_id!r}"
            raise ValueError(message)
        item = updated.hypotheses[index]
        if updated.active_hypothesis_id == change.hypothesis_id:
            message = f"cannot {change.disposition} active hypothesis {change.hypothesis_id!r}"
            raise ValueError(message)
        if not item.rounds:
            message = f"cannot update incomplete hypothesis {change.hypothesis_id!r}"
            raise ValueError(message)
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
        message = "round hypothesis_id must match its owning hypothesis"
        raise ValueError(message)
    if any(item.round_number == record.round_number for item in hypothesis.rounds):
        _exception_message = f"round {record.round_number} already belongs to hypothesis"
        raise ValueError(_exception_message)
    updated = hypothesis.model_copy(
        update={"rounds": [*hypothesis.rounds, record]},
        deep=True,
    )
    updated.declared_outcome = _declared_outcome(record.hypothesis_declared_outcome)
    updated.review = _review(record)
    measurement = _measurement(record, prior_rounds, space)
    comparison = round_comparison(record)
    updated.resolution = resolve_hypothesis_outcome(
        ResolutionEvidence(
            declared=updated.declared_outcome,
            passed=record.passed,
            reviewed=updated.review not in {HypothesisReview.PENDING, HypothesisReview.DEFERRED},
            comparison=comparison,
        )
    )
    if measurement is not None:
        updated.measurement = measurement
    retained = record.candidate_retained
    if retained is not None:
        updated.candidate_retained = retained
    return Hypothesis.model_validate(updated.model_dump())


def round_comparison(record: RoundRecord) -> MetricComparison | None:
    """Return how one round's headline reading compared with its baseline.

    The writer decides and stores this comparison once. Later projections do
    not recompute historical decisions under a different metric space.
    """
    if not record.official_evaluation or record.perf_metric is None:
        return None
    if not trusted_perf_provenance(record.perf_provenance):
        return None
    return record.perf_comparison


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
            message = (
                f"round {record.round_number} names unknown hypothesis {record.hypothesis_id!r}"
            )
            raise ValueError(message)
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


def _declared_outcome(value: str | None) -> HypothesisOutcome | None:
    if value is None:
        return None
    return HypothesisOutcome(value)


def _review(record: RoundRecord) -> HypothesisReview:
    if record.judge_verdict is None:
        message = "round record requires judge_verdict"
        raise ValueError(message)
    return HypothesisReview(record.judge_verdict)


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
    baseline_round = record.perf_baseline_round
    baseline_commit = record.perf_baseline_commit
    baseline_value = record.perf_baseline_metric
    delta = record.perf_delta_pct
    delta_reason = None
    if (
        delta is None
        and baseline_round is None
        and baseline_commit is None
        and baseline_value is None
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


# --- Evidence: retention, frontier, and carry-over notices ---


@dataclass(frozen=True, slots=True)
class CarryOver:
    """Record-derived guidance passed to the next planning turn.

    Rebuilt from round records on resume (:meth:`HypothesisSearch.initial_carry`),
    never persisted.
    """

    regression: RegressionNotice | None = None
    exhaustion: ExhaustionNotice | None = None


def record_candidate_metrics(record: RoundRecord) -> dict[str, float]:
    """Return the comparable objective row associated with *record*."""
    if record.official_evaluation and record.metrics:
        return record.metrics
    if record_candidate_retained(record) is True:
        return record.candidate_metrics
    return {}


def record_candidate_retained(record: RoundRecord) -> bool | None:
    """Read the framework's recorded checkpoint-retention decision."""
    return record.candidate_retained


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


def _archive_metrics(metrics: dict[str, float], space: MetricSpace) -> tuple[ArchiveMetric, ...]:
    return tuple(
        ArchiveMetric(
            name=objective.name, value=metrics[objective.name], direction=objective.direction
        )
        for objective in space.objectives
        if objective.name in metrics
    )


def _archive_latest(latest: RoundRecord, space: MetricSpace) -> ArchiveLatestRound:
    trusted_official = latest.official_evaluation and trusted_perf_provenance(
        latest.perf_provenance
    )
    official_metrics: tuple[ArchiveMetric, ...] = ()
    official_scalar: ArchiveScalarReading | None = None
    if trusted_official and space.objectives and space.complete(latest.metrics):
        official_metrics = _archive_metrics(latest.metrics, space)
    elif trusted_official and latest.perf_metric is not None:
        official_scalar = ArchiveScalarReading(value=latest.perf_metric, unit=latest.perf_unit)
    return ArchiveLatestRound(
        round_number=latest.round_number,
        commit=latest.commit,
        official_metrics=official_metrics,
        official_scalar=official_scalar,
        retained=record_candidate_retained(latest),
    )


def pareto_archive_view(records: Sequence[RoundRecord], space: MetricSpace) -> ParetoArchiveView:
    """Select trusted frontier parents and any measured points awaiting review."""
    latest = max(records, key=lambda record: record.round_number, default=None)
    view = ParetoArchiveView(
        axes=tuple(
            ArchiveAxis(name=objective.name, direction=objective.direction)
            for objective in space.objectives
        ),
        relative_noise=space.relative_noise,
        latest=None if latest is None else _archive_latest(latest, space),
    )
    if not space.objectives:
        return view

    trusted_parents = tuple(
        ArchiveTrustedParent(
            round_number=record.round_number,
            commit=record.commit,
            official=record.official_evaluation,
            metrics=_archive_metrics(record_candidate_metrics(record), space),
            operating_point=record.candidate_operating_point,
            artifact=record.candidate_evaluation_artifact or record.evaluation_artifact,
        )
        for record in pareto_frontier_records(records, space)
        if record.commit is not None
    )
    trusted_rounds = {record.round_number for record in trusted_candidate_records(records, space)}
    pending = sorted(
        (
            record
            for record in records
            if record.round_number not in trusted_rounds
            and record.commit
            and record_candidate_retained(record) is True
            and all(objective.name in record.candidate_metrics for objective in space.objectives)
        ),
        key=lambda record: record.round_number,
    )
    omitted = pending[:-_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT]
    return view.model_copy(
        update={
            "trusted_parents": trusted_parents,
            "pending_claims": tuple(
                ArchivePendingClaim(
                    round_number=record.round_number,
                    commit=record.commit,
                    metrics=_archive_metrics(record.candidate_metrics, space),
                    operating_point=record.candidate_operating_point,
                    artifact=record.candidate_evaluation_artifact,
                    reason=record.candidate_retention_reason,
                )
                for record in pending[-_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT:]
                if record.commit is not None
            ),
            "omitted_claims": (
                OmittedPendingClaims(
                    count=len(omitted),
                    first_round=omitted[0].round_number,
                    last_round=omitted[-1].round_number,
                )
                if omitted
                else None
            ),
        }
    )


def pareto_archive_conflict(
    *,
    candidate_disposition: CandidateDisposition,
    candidate_metrics: dict[str, float],
    records: Sequence[RoundRecord],
    space: MetricSpace,
) -> ArchiveConflict | None:
    """Return the live archive points that dominate a claimed frontier row."""
    if candidate_disposition is not CandidateDisposition.PARETO_FRONTIER:
        return None
    dominators = pareto_archive_dominators(candidate_metrics, records, space)
    if not dominators:
        return None
    return ArchiveConflict(
        dominators=tuple(
            ArchiveDominator(
                round_number=record.round_number,
                metrics=_archive_metrics(record_candidate_metrics(record), space),
            )
            for record in dominators
        )
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
        f"{rounds[0]}{_EN_DASH}{rounds[-1]}) all landed in {lo:.2f}{_EN_DASH}{hi:.2f}{unit_suffix} "
        f"— a {spread_pct:.2f}% spread, well within bench noise. Whatever you've "
        f"been working on for those rounds is not actually moving the headline metric."
    )


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


def terminal_workspace_notice(
    records: Sequence[RoundRecord],
) -> RetainedTerminalCheckpoint | TerminalWorkspaceEdits | None:
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
        return RetainedTerminalCheckpoint(
            hypothesis_id=latest.hypothesis_id,
            outcome=latest.hypothesis_outcome,
            round_number=latest.round_number,
            reviewed=latest.passed and latest.reviewed,
            candidate_metrics=latest.candidate_metrics,
            commit=latest.commit,
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
    latest_checkpoint = next(
        (
            record
            for record in reversed(records[:-1])
            if record.commit is not None
            and record.hypothesis_outcome in {HypothesisOutcome.CONTINUE.value, "proven"}
        ),
        None,
    )
    checkpoint = (
        WorkspaceCheckpoint(
            round_number=latest_checkpoint.round_number,
            outcome=latest_checkpoint.hypothesis_outcome,
            reviewed=latest_checkpoint.reviewed,
        )
        if latest_checkpoint is not None and latest_checkpoint.round_number != parent_round
        else None
    )
    return TerminalWorkspaceEdits(
        hypothesis_id=latest.hypothesis_id,
        outcome=latest.hypothesis_outcome,
        round_number=latest.round_number,
        parent_round=parent_round,
        checkpoint=checkpoint,
    )
