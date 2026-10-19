"""Read model of a dynamic run: the search's published record in the shape frontends consume.

The search publishes only scientific state (hypotheses, rounds, workstreams, baseline, leader).
This module copies those facts into `AgentRunProjection` without inferring any resolution.
Ranking facts come from the metric rows the search accepted from trusted evaluation evidence,
so every measured round is an official evaluation and no provisional number can reach the
frontier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vibesys.candidates.api import failure, reading
from vibesys.hypothesis import derive_hypothesis_title
from vibesys.hypothesis.readmodel import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
)
from vibesys.orchestration.dynamic.search.api import FailureKind, workstream_key
from vibesys.run.contracts import (
    IterationKind,
    IterationMetric,
    IterationStatus,
    IterationSummary,
    IterationTree,
    InputFailureSummary,
    PluginProjection,
    RoundFailureSummary,
    RoundSummary,
)

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.search.api import (
        Failure,
        FinishedWork,
        HypothesisRecord,
        MetricRow,
        RoundRecord,
        RunSnapshot,
    )

type Verdict = Literal["pass", "fail", "skipped"]


def project_run_snapshot(snapshot: RunSnapshot, *, experiment_revision: int) -> PluginProjection:
    """Project one published record; `experiment_revision` is the core revision it was read at."""
    rounds = sorted(
        (record for item in snapshot.hypotheses for record in item.rounds),
        key=lambda record: record.sequence,
    )
    in_flight = tuple(item.plan.work_id for item in snapshot.running)
    projection = AgentRunProjection(
        current_round=len(rounds),
        objectives=tuple(f"{row.name}:{row.direction}" for row in _baseline_metrics(snapshot)),
        active_hypothesis_id=in_flight[-1] if in_flight else None,
        experiment_revision=experiment_revision,
        hypotheses=[
            _hypothesis_view(item, active=item.hypothesis_id in in_flight, snapshot=snapshot)
            for item in snapshot.hypotheses
        ],
        rounds=[_round_view(record) for record in rounds],
    )
    return PluginProjection(
        payload=projection.model_dump(mode="json"),
        iterations=_workstream_iterations(snapshot),
        rounds=tuple(
            _summary(view, record) for view, record in zip(projection.rounds, rounds, strict=True)
        ),
        experiment_revision=experiment_revision,
        input_failure=_input_failure(snapshot),
    )


def _input_failure(snapshot: RunSnapshot) -> InputFailureSummary | None:
    """Why the input failed its trusted measurement, once it has; None while pending or trusted."""
    why = None if snapshot.baseline is None else failure(snapshot.baseline)
    if why is None:
        return None
    return InputFailureSummary(kind=why.kind.value, stage=why.stage, output=why.output)


def _baseline_metrics(snapshot: RunSnapshot) -> tuple[MetricRow, ...]:
    found = None if snapshot.baseline is None else reading(snapshot.baseline)
    return () if found is None else found.metrics


def _workstream_iterations(snapshot: RunSnapshot) -> IterationTree:
    """Expose workstreams in the order they were scheduled, whatever order they completed in.

    A node is named by the workstream's key (its schedule position and work ID), never by the
    work ID alone: a hypothesis that goes on (``continue_hypothesis``) runs as a new workstream
    under the same planner-chosen ID, and each of those workstreams is its own node.
    """
    rounds = {record.sequence: record for item in snapshot.hypotheses for record in item.rounds}
    nodes: list[IterationSummary] = []
    for sequence, work_id, title, finished in sorted(
        (
            *(
                (item.sequence, item.plan.work_id, item.plan.title, item)
                for item in snapshot.finished
            ),
            *(
                (item.sequence, item.plan.work_id, item.plan.title, None)
                for item in snapshot.running
            ),
        ),
        key=lambda entry: entry[0],
    ):
        record = rounds.get(sequence)
        root_id = f"workstream:{workstream_key(sequence, work_id)}"
        status = _workstream_status(finished, record)
        nodes.append(
            IterationSummary(
                id=root_id,
                kind=IterationKind.WORKSTREAM,
                label=title or work_id,
                ordinal=sequence,
                status=status,
                child_count=1 if record is not None else 0,
                headline_metrics=_headline_metrics(_headline(record.metrics) if record else None),
            )
        )
        if record is not None:
            nodes.append(
                IterationSummary(
                    id=record.attempt.root,
                    parent_id=root_id,
                    kind=IterationKind.ATTEMPT,
                    label=f"Attempt {sequence}",
                    ordinal=sequence,
                    status=status,
                    headline_metrics=_headline_metrics(_headline(record.metrics)),
                )
            )
    return IterationTree.from_nodes(tuple(nodes))


def _workstream_status(
    finished: FinishedWork | None, record: RoundRecord | None
) -> IterationStatus:
    if finished is None:
        return IterationStatus.ACTIVE
    failure = finished.failure if record is None else record.failure or finished.failure
    if failure is not None:
        return (
            IterationStatus.CANCELLED
            if failure.kind is FailureKind.STOPPED
            else IterationStatus.FAILED
        )
    if record is not None and record.review_passed is False:
        return IterationStatus.FAILED
    return IterationStatus.COMPLETED


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


def _failure_text(failure: Failure) -> str:
    """The implementer's own prose when there is one, else the typed reason's label.

    ``Failure``'s fields are closed per kind (``_values._FIELDS``), so at most one of these
    is ever set for a given failure; the order only matters for which one a reader sees first
    when more than one could apply.
    """
    if failure.output:
        return failure.output
    if failure.blocker is not None:
        parts = (failure.blocker.summary, failure.blocker.next_step)
        text = "\n\n".join(part for part in parts if part)
        if text:
            return text
    for reason in (
        failure.cause,
        failure.turn_failure,
        failure.ended,
        failure.refusal,
        failure.declined,
    ):
        if reason is not None:
            return reason.value
    return ""


def _operator_failure_text(failure: Failure) -> str:
    """Project evaluator output safely without changing the policy's captured text."""
    if failure.operator_output is not None:
        return failure.operator_output
    if failure.output:
        return failure.kind.value
    return _failure_text(failure)


def _round_failure_summary(failure: Failure | None) -> RoundFailureSummary | None:
    """The round's typed failure, in the strategy-neutral shape ``WORKSTREAM_FINISHED`` reads."""
    if failure is None:
        return None
    return RoundFailureSummary(
        kind=failure.kind.value,
        role=failure.role.value if failure.role is not None else None,
        turns=failure.turns,
        limit=failure.limit,
        text=_failure_text(failure),
        operator_text=_operator_failure_text(failure),
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
        last_error=_round_failure_summary(record.failure),
        repeated_error=record.failure is not None
        and record.failure.kind is FailureKind.REPEATED_TURN,
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


def _hypothesis_view(
    item: HypothesisRecord, *, active: bool, snapshot: RunSnapshot
) -> HypothesisView:
    rounds = item.rounds
    measured = next((record for record in reversed(rounds) if record.metrics), None)
    headline = _headline(measured.metrics) if measured is not None else None
    base = _headline(_baseline_metrics(snapshot))
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
        best=True if snapshot.leading == item.hypothesis_id else None,
        strategy_disposition=item.strategy.value,
        strategy_reason=item.reason or None,
        active=active,
        last_experiment_revision=0,
    )
