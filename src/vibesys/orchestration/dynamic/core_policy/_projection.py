"""Read model of a dynamic run: the strategy state in the shape frontends already consume.

The strategy keeps only scientific state (hypotheses, rounds, baseline, winner).
This module copies those facts into `AgentRunProjection` without inferring any
resolution. Ranking facts come from the metric rows the strategy accepted from
trusted evaluation evidence, so every measured round is an official evaluation
and no provisional number can reach the frontier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vibesys.hypothesis import derive_hypothesis_title
from vibesys.hypothesis.readmodel import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
)
from vibesys.run.contracts import (
    IterationKind,
    IterationMetric,
    IterationStatus,
    IterationSummary,
    IterationTree,
    PluginProjection,
    RoundSummary,
)

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.strategy.api import (
        AttemptRecord,
        DynamicStrategyState,
        HypothesisRecord,
        MetricRow,
        RoundRecord,
        Winner,
    )

type Verdict = Literal["pass", "fail", "skipped"]


def project_strategy_state(
    state: DynamicStrategyState, *, experiment_revision: int
) -> PluginProjection:
    """Project one strategy state; `experiment_revision` is the core revision it was read at."""
    rounds = sorted(
        (record for item in state.hypotheses for record in item.rounds),
        key=lambda record: record.sequence,
    )
    in_flight = tuple(
        record.plan.work_id
        for record in sorted(state.attempts, key=lambda record: record.sequence)
        if record.phase.value != "done" and not record.withdrawn
    )
    active = in_flight[-1] if in_flight else None
    projection = AgentRunProjection(
        current_round=len(rounds),
        objectives=tuple(f"{row.name}:{row.direction}" for row in state.baseline.metrics),
        active_hypothesis_id=active,
        experiment_revision=experiment_revision,
        hypotheses=[
            _hypothesis_view(item, active=item.hypothesis_id in in_flight, state=state)
            for item in state.hypotheses
        ],
        rounds=[_round_view(record) for record in rounds],
    )
    return PluginProjection(
        payload=projection.model_dump(mode="json"),
        iterations=_workstream_iterations(state),
        rounds=tuple(
            _summary(view, record) for view, record in zip(projection.rounds, rounds, strict=True)
        ),
        experiment_revision=experiment_revision,
    )


def _workstream_iterations(state: DynamicStrategyState) -> IterationTree:
    """Expose workstreams and attempts in scheduler order, independent of completion order."""
    workstreams: dict[str, list[AttemptRecord]] = {}
    for record in sorted(state.attempts, key=lambda item: item.sequence):
        workstreams.setdefault(record.plan.work_id, []).append(record)
    nodes: list[IterationSummary] = []
    for work_id, attempts in workstreams.items():
        root_id = f"workstream:{work_id}"
        latest = attempts[-1]
        root_headline = _attempt_headline(latest)
        nodes.append(
            IterationSummary(
                id=root_id,
                kind=IterationKind.WORKSTREAM,
                label=latest.plan.title or work_id,
                ordinal=attempts[0].sequence,
                status=_workstream_status(attempts),
                child_count=len(attempts),
                headline_metrics=_headline_metrics(root_headline),
            )
        )
        nodes.extend(
            IterationSummary(
                id=record.attempt.root,
                parent_id=root_id,
                kind=IterationKind.ATTEMPT,
                label=f"Attempt {record.sequence}",
                ordinal=record.sequence,
                status=_attempt_status(record),
                headline_metrics=_headline_metrics(_attempt_headline(record)),
            )
            for record in attempts
        )
    return IterationTree.from_nodes(tuple(nodes))


def _attempt_status(record: AttemptRecord) -> IterationStatus:
    if record.withdrawn:
        return IterationStatus.CANCELLED
    if record.phase.value != "done":
        return IterationStatus.ACTIVE
    if record.failure is not None or record.review_passed is False:
        return IterationStatus.FAILED
    return IterationStatus.COMPLETED


def _workstream_status(attempts: list[AttemptRecord]) -> IterationStatus:
    statuses = tuple(_attempt_status(record) for record in attempts)
    if IterationStatus.ACTIVE in statuses:
        return IterationStatus.ACTIVE
    if IterationStatus.COMPLETED in statuses:
        return IterationStatus.COMPLETED
    if statuses and all(status is IterationStatus.CANCELLED for status in statuses):
        return IterationStatus.CANCELLED
    return IterationStatus.FAILED


def _attempt_headline(record: AttemptRecord) -> MetricRow | None:
    return next(
        (
            reading.headline()
            for reading in reversed(record.readings)
            if reading.headline() is not None
        ),
        None,
    )


def _headline_metrics(headline: MetricRow | None) -> tuple[IterationMetric, ...]:
    if headline is None:
        return ()
    return (
        IterationMetric(
            id=headline.name,
            value=headline.value,
            unit=headline.unit,
            direction=headline.direction,
        ),
    )


def _headline(rows: tuple[MetricRow, ...]) -> MetricRow | None:
    return rows[0] if rows else None


def _judged(record: RoundRecord) -> Literal["pass", "fail"] | None:
    """The review verdict, or None when no judge reviewed the round."""
    if record.review_passed is None:
        return None
    return "pass" if record.review_passed else "fail"


def _verdict(record: RoundRecord) -> Verdict:
    return _judged(record) or "skipped"


def _commit(record: RoundRecord) -> str | None:
    return record.candidate.git_commit if record.candidate is not None else None


def _round_view(record: RoundRecord) -> RoundView:
    headline = _headline(record.metrics)
    return RoundView(
        round_number=record.sequence,
        commit=_commit(record),
        perf_metric=headline.value if headline is not None else None,
        perf_unit=headline.unit if headline is not None else None,
        passed=(
            record.failure is None
            and record.review_passed is not False
            and record.accuracy_passed is not False
            and record.benchmark_passed is not False
        ),
        official_evaluation=headline is not None,
        judge_verdict=_verdict(record),
    )


def _summary(view: RoundView, record: RoundRecord) -> RoundSummary:
    """The round as a run event: failed when the review failed it or the workstream did.

    A workstream that failed after a passing review (its measurement produced no evidence)
    has no metric; reporting it as completed would leave a pass with a null metric and no
    sign of why.
    """
    return RoundSummary(
        number=view.round_number,
        status="failed"
        if view.judge_verdict == "fail" or record.failure is not None
        else "completed",
        attempts=view.attempts,
        judge_verdict=view.judge_verdict,
        perf_metric=view.perf_metric,
        perf_unit=view.perf_unit,
        profile_skipped=view.profile_skipped,
    )


def _round_in_hypothesis(record: RoundRecord) -> HypothesisRoundView:
    headline = _headline(record.metrics)
    return HypothesisRoundView(
        round_number=record.sequence,
        passed=record.failure is None and record.review_passed is not False,
        reviewed=record.review_passed is not None,
        hypothesis_outcome=record.outcome.value if record.outcome is not None else None,
        judge_verdict=_judged(record),
        perf_metric=headline.value if headline is not None else None,
        perf_unit=headline.unit if headline is not None else None,
        commit=_commit(record),
        official_evaluation=headline is not None,
    )


def _delta_pct(value: float, baseline: float) -> float | None:
    return None if baseline == 0 else (value - baseline) / abs(baseline) * 100.0


def _winner_hypothesis(winner: Winner | None) -> str | None:
    return winner.hypothesis_id if winner is not None else None


def _hypothesis_view(
    item: HypothesisRecord, *, active: bool, state: DynamicStrategyState
) -> HypothesisView:
    rounds = item.rounds
    measured = next((record for record in reversed(rounds) if record.metrics), None)
    headline = _headline(measured.metrics) if measured is not None else None
    base = _headline(state.baseline.metrics)
    comparable = (
        base if base is not None and headline is not None and base.name == headline.name else None
    )
    reviewed = next(
        (record for record in reversed(rounds) if record.review_passed is not None), None
    )
    return HypothesisView(
        hypothesis_id=item.hypothesis_id,
        title=item.title or derive_hypothesis_title(item.hypothesis),
        claim=item.hypothesis or None,
        action=item.last_task or None,
        first_round=item.first_sequence,
        last_round=rounds[-1].sequence if rounds else item.first_sequence,
        rounds=[_round_in_hypothesis(record) for record in rounds],
        resolved_outcome=(
            rounds[-1].outcome.value if rounds and rounds[-1].outcome is not None else None
        ),
        judge_verdict=_judged(reviewed) if reviewed is not None else None,
        perf_metric=headline.value if headline is not None else None,
        perf_unit=headline.unit if headline is not None else None,
        perf_delta_pct=(
            _delta_pct(headline.value, comparable.value)
            if headline is not None and comparable is not None
            else None
        ),
        perf_metric_round=measured.sequence if measured is not None else None,
        perf_metric_name=headline.name if headline is not None else None,
        perf_direction=headline.direction if headline is not None else None,
        perf_baseline_value=comparable.value if comparable is not None else None,
        kept=True if _winner_hypothesis(state.winner) == item.hypothesis_id else None,
        strategy_disposition=item.strategy.value,
        strategy_reason=item.reason or None,
        active=active,
        last_experiment_revision=0,
    )
