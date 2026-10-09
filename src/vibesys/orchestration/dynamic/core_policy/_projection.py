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
from vibesys.run.contracts import PluginProjection, RoundSummary

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.strategy.api import (
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
        rounds=tuple(_summary(view) for view in projection.rounds),
        experiment_revision=experiment_revision,
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


def _summary(view: RoundView) -> RoundSummary:
    return RoundSummary(
        number=view.round_number,
        status="failed" if view.judge_verdict == "fail" else "completed",
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
