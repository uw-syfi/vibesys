"""Pure transitions for the authoritative agent-run hypothesis state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from vibesys.loops.agent.model import (
    AgentRunState,
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisStrategy,
)
from vibesys.loops.metrics import Measurement, MetricComparison, MetricSpace
from vibesys.schemas import (
    HypothesisOutcome,
    HypothesisStrategyUpdate,
    OrchestratorPlan,
    PerfDeltaReason,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_loop_state import PerfProvenance, RoundRecord


def trusted_perf_provenance(provenance: PerfProvenance | None) -> bool:
    """Whether a round's headline metric may drive a framework decision.

    The one trust rule for :data:`~vs_loop_state.PerfProvenance`, shared by
    hypothesis resolution, scalar and Pareto retention, the recorded delta, and
    trusted Pareto-parent selection. Legacy records carry no provenance and stay
    trusted, so reprojecting an old run does not rewrite its historical
    resolutions; only an explicit agent self-report is untrusted.
    """
    return provenance != "implementer"


@dataclass(frozen=True)
class ResolutionEvidence:
    """Inputs the framework needs to finalize one hypothesis declaration.

    ``comparison`` is the round's headline reading ordered against its causal
    baseline, and it is set only for a reading the framework measured itself.
    ``None`` therefore covers both "no official metric was recorded" and "the
    number on the record is the implementer's own report", which is a
    different fact from ``MetricComparison.INCOMPARABLE``: there a trusted
    reading exists but could not be ordered. Resolution consumes the
    comparison and never the readings, the axis direction, or the measurement
    tolerance behind it.
    """

    declared: HypothesisOutcome | None
    passed: bool
    reviewed: bool
    comparison: MetricComparison | None


def resolve_hypothesis_outcome(
    evidence: ResolutionEvidence,
) -> HypothesisResolution | None:
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
            if evidence.comparison is None:
                resolution = HypothesisResolution.UNMEASURED
            else:
                resolution = _resolve_metric_evidence(evidence.comparison)
    return resolution


def _resolve_metric_evidence(comparison: MetricComparison) -> HypothesisResolution:
    match comparison:
        case MetricComparison.BETTER:
            return HypothesisResolution.PROVEN
        case MetricComparison.WORSE:
            return HypothesisResolution.DISPROVEN
        case MetricComparison.WITHIN_NOISE | MetricComparison.INCOMPARABLE:
            # A sub-noise delta and an unmeasurable one are both "the evidence
            # does not decide this hypothesis".
            return HypothesisResolution.INCONCLUSIVE
    assert_never(comparison)


def scalar_candidate_retained(comparison: MetricComparison) -> bool | None:
    """Return whether an official scalar candidate advances the best checkpoint.

    *comparison* orders the candidate against the best prior official reading,
    which callers obtain from ``MetricSpace.compare_to_best``. An empty history
    compares as ``BETTER`` there, so the first trusted checkpoint is retained.
    """
    match comparison:
        case MetricComparison.BETTER:
            return True
        case MetricComparison.WORSE | MetricComparison.WITHIN_NOISE:
            # Retention requires a real advance: matching the best checkpoint
            # within noise is not one.
            return False
        case MetricComparison.INCOMPARABLE:
            return None


def record_metric_value(record: RoundRecord, metric: str | None) -> float | None:
    """Read one official metric off *record* without guessing across axes.

    The objective row wins when it names *metric*, which is what lets a record
    whose headline unit was later renamed still answer for the axis it
    measured. The headline scalar answers only when it is the axis asked for,
    or when the record carries no row to disagree with.
    """
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
    """Find the baseline for one metric, preferring the exact causal parent.

    The parent of an official round is usually a provisional round whenever
    ``official_eval_every`` exceeds 1, so an exact parent match rarely exists.
    Fall back to the newest trusted official measurement that does not
    postdate the parent round; comparing against a later measurement would
    invert cause and effect.
    """
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
        # The caller named a causal parent, no trusted official round carries
        # that commit, and there is no round number to bound the fallback by.
        # Falling through to the newest measurement here would let a round that
        # came *after* the parent serve as its baseline, inverting cause and
        # effect. Fail closed instead.
        return None
    return comparable[-1]


def baseline_candidates(
    *,
    metric: str | None,
    rounds: Sequence[RoundRecord],
) -> list[RoundRecord]:
    """Return the rounds admissible as a baseline for *metric*, in order.

    The one admissibility rule behind ``metric_baseline``: a trusted official
    measurement that carries the axis, whether as its headline unit or in its
    objective row. Whether this set is empty is what separates "no baseline
    existed yet" from "a baseline existed but could not be resolved".
    """
    return [
        item
        for item in rounds
        if item.official_evaluation
        and item.perf_metric is not None
        and trusted_perf_provenance(item.perf_provenance)
        and (item.perf_unit == metric or (metric is not None and metric in item.metrics))
    ]


def measurement_delta_reason(hypothesis: Hypothesis) -> PerfDeltaReason | None:
    """Why *hypothesis*'s headline number carries no causal delta.

    ``None`` when a delta exists, when no official metric was recorded at
    all, or when the evidence predates provenance tracking. A hypothesis
    whose only official headline is the implementer's own report has no
    measurement to carry the reason, so it is answered from the rounds.
    """
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
    state: AgentRunState,
    plan: OrchestratorPlan,
    *,
    started_round: int,
    parent_round: int | None = None,
    parent_commit: str | None = None,
) -> AgentRunState:
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


def update_active_hypothesis(
    state: AgentRunState,
    hypothesis: Hypothesis,
) -> AgentRunState:
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
    state: AgentRunState,
    record: RoundRecord,
    *,
    keep_active: bool,
) -> AgentRunState:
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


def finish_hypothesis(state: AgentRunState) -> AgentRunState:
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
    state: AgentRunState,
    updates: Sequence[HypothesisStrategyUpdate],
) -> AgentRunState:
    """Apply orchestrator-owned parked/abandoned decisions."""
    updated = state.clone()
    seen: set[str] = set()
    for change in updates:
        if change.hypothesis_id in seen:
            raise ValueError(  # noqa: TRY003  # tracked: #288
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
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"strategy update names unknown hypothesis {change.hypothesis_id!r}"
            )
        item = updated.hypotheses[index]
        if updated.active_hypothesis_id == change.hypothesis_id:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"cannot {change.disposition} active hypothesis {change.hypothesis_id!r}"
            )
        if not item.rounds:
            raise ValueError(  # noqa: TRY003  # tracked: #288
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
    """Return a hypothesis updated with one completed round's evidence.

    The round's own ``perf_comparison`` decides its resolution. *space* is the
    owning run's persisted metric space; callers holding an ``AgentRunState``
    pass ``state.metrics``. It is used to re-derive a comparison for records
    written before the framework stored one, and to order retention against
    the best prior official reading.
    """
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

    ``None`` means there is nothing to order: the round recorded no official
    metric, or the number it recorded is the implementer's own report. A record
    that carries ``perf_comparison`` answers for itself: the framework decided
    it once, when the round was written, and re-deriving it later from a
    possibly re-configured space would let a resumed run disagree with what it
    recorded. Older records have the comparison derived from *space* instead.

    The provenance guard has to come before that re-derivation. A round the
    implementer self-reported stores no comparison, which is indistinguishable
    from a pre-#579 record; without this guard the fallback would re-derive one
    from the space and resume, and the server read path, would resolve the
    hypothesis INCONCLUSIVE where the loop resolved it UNMEASURED.
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


def _reading(
    measurement: HypothesisMeasurement,
    value: float | None,
) -> Measurement | None:
    if value is None:
        return None
    return Measurement(
        metric=measurement.metric,
        value=value,
        direction=measurement.direction,
    )


def adopt_metric_space(state: AgentRunState, space: MetricSpace) -> AgentRunState:
    """Record the run's metric space and re-derive evidence within it.

    Called once when a run starts, with the axes and tolerance the task
    declares. Every later reader takes the space from the returned state, so a
    resumed run, its persisted hypothesis resolutions, and the server read path
    agree without any of them re-reading the task file.
    """
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


def _advance_experiment_revision(state: AgentRunState, hypothesis_ids: set[str]) -> None:
    """Advance the persisted projection revision and mark its changed rows."""
    state.experiment_revision += 1
    for hypothesis in state.hypotheses:
        if hypothesis.hypothesis_id in hypothesis_ids:
            hypothesis.last_experiment_revision = state.experiment_revision


def reproject_run_evidence(state: AgentRunState) -> AgentRunState:
    """Rebuild hypothesis summaries from their authoritative round evidence.

    Every round answers with the comparison it recorded, and ``state.metrics``
    supplies the space for records written before the framework stored one, so
    reprojection on ``--resume`` and in the server read path reproduces exactly
    what the loop recorded.
    """
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
            raise ValueError(  # noqa: TRY003  # tracked: #288
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


def _validated_state(state: AgentRunState) -> AgentRunState:
    return AgentRunState.model_validate(state.model_dump())


def _correct_legacy_resolution(
    hypothesis: Hypothesis,
    record: RoundRecord,
    measurement: HypothesisMeasurement | None,
    comparison: MetricComparison | None,
) -> None:
    """Re-decide a legacy record's self-declared ``proven`` from its evidence.

    Records written before the framework resolved outcomes carry the
    implementer's own label. Only the measurement may overrule it, and it does
    so through the same comparison every other reader consumes.
    """
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
            metric=record.perf_unit,
            value=record.perf_metric,
            direction=record.perf_direction,
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
    # The commit of the record actually used as the baseline, which the
    # fallback makes distinct from the hypothesis's parent commit: with
    # `official_eval_every > 1` the causal parent is usually a provisional
    # round, and the baseline is an earlier official one.
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
        # A legacy record carries no provenance and keeps reading as a
        # deliberate absolute; only provenance-era evidence is labelled.
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
    elif not record.official_evaluation or record.perf_metric is None:
        retained = None
    elif record.perf_unit is None:
        # An official reading with no axis cannot be ordered against anything.
        retained = None
    else:
        candidate = Measurement(
            metric=record.perf_unit,
            value=record.perf_metric,
            direction=record.perf_direction,
        )
        axis = space.direction(candidate)
        # Same admissibility rule as `metric_baseline`: a prior round counts
        # when it carries this axis, whether as its headline unit or in its
        # objective row. Matching on the headline unit alone would silently
        # drop a round whose unit was later renamed, and retention would then
        # compare against a shorter history than resolution did.
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
