"""Recording workstream results as rounds, and what the planner sees of them."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.models import WorkstreamPhase
from vibesys.orchestration.hypothesis import (
    HypothesisConfig,
    HypothesisSearch,
    normalize_hypothesis_title,
)
from vibesys.orchestration.hypothesis import transitions as hypothesis_transitions
from vibesys.orchestration.metrics import Measurement
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome, RoundRecord

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from vibesys.orchestration.dynamic.input_gate import InputGate
    from vibesys.orchestration.dynamic.models import (
        DynamicOptions,
        DynamicState,
        DynamicWorkstream,
        EvaluationResult,
        EvidenceReference,
        ReviewResult,
    )
    from vs_runtime.api import AgentEvaluation

_MAX_HISTORY_ROWS = 16
_MAX_HISTORY_METRICS = 8
_MAX_HISTORY_REVISION_CHARS = 256
_MAX_HISTORY_METRIC_NAME_CHARS = 128
_MAX_HISTORY_METRIC_UNIT_CHARS = 64
_MAX_HISTORY_SUMMARY_CHARS = 600
_MAX_HISTORY_REVIEW_CHARS = 600
_MAX_HISTORY_NEXT_STEP_CHARS = 600
# Evaluations shown for a running implementer turn, and the end of each failure.
_MAX_LIVE_EVALUATIONS = 4
_MAX_LIVE_FAILURE_CHARS = 400


class Rounds:
    """Records each finished workstream as a round and selects the winner.

    Every candidate decision is made against the input reading of ``gate``;
    the winner is re-filtered at selection because a round recorded before
    the input was measured was not gated. The planner's view of the history
    (bounded rows and the input reading) is projected here too.
    """

    def __init__(
        self,
        options: DynamicOptions,
        state: DynamicState,
        gate: InputGate,
        *,
        lock: asyncio.Lock,
        commit: Callable[[str], Awaitable[None]],
    ) -> None:
        """Bind the round book to one run's state, input gate and commit path."""
        self.options = options
        self.state = state
        self._gate = gate
        self._lock = lock
        self._commit = commit

    def planner_context(
        self, live: Mapping[str, Sequence[AgentEvaluation]] | None = None
    ) -> dict[str, str]:
        """Return the history and input facts every planning prompt states.

        ``live`` maps each running implementer turn to the evaluations it has
        submitted so far; its row shows them in place of the facts of the
        attempt before it.
        """
        baseline = self.state.baseline
        return {
            "baseline": (
                json.dumps(_compact_evaluation(baseline), separators=(",", ":"))
                if baseline is not None and baseline.benchmark_passed
                else ""
            ),
            "input_failure": (
                (baseline.benchmark_feedback or "no feedback provided")[:_MAX_HISTORY_REVIEW_CHARS]
                if baseline is not None and not baseline.benchmark_passed
                else ""
            ),
            "history": self._history_projection(live or {}),
            "buildable": json.dumps(
                [_buildable_row(item) for item in self.buildable()], separators=(",", ":")
            ),
            "older_ids": ", ".join(
                item.hypothesis_id for item in self.state.workstreams[:-_MAX_HISTORY_ROWS]
            ),
        }

    def buildable(self) -> tuple[DynamicWorkstream, ...]:
        """Return the finished workstreams a new workstream may start from.

        A candidate qualifies when its trusted evaluation, of exactly its
        latest revision, passed accuracy. Its benchmark may have failed: work
        that is correct but not yet fast enough is still worth building on,
        and rebuilding it in every sibling wastes their turns. The adopted
        base revision stays the default parent.
        """
        return tuple(
            item
            for item in self.state.workstreams
            if item.phase is not WorkstreamPhase.IMPLEMENTING
            and item.evaluation is not None
            and item.evaluation.accuracy_passed is True
            and item.evaluation.revision == item.candidate_revision
        )

    async def record(self, index: int) -> None:
        """Commit one workstream result through shared hypothesis transitions."""
        if self.state.workstreams[index].evaluation is not None:
            # The candidate decision compares against the input measurement.
            await self._gate.measured()
        async with self._lock:
            item = self.state.workstreams[index]
            implementation = item.implementation
            # A slot given up before any implementer turn returned still ends
            # its hypothesis: without a round the hypothesis stays incomplete,
            # and the planner, told the slot failed, could not abandon it.
            given_up = implementation is None and item.phase is WorkstreamPhase.FAILED
            if (implementation is None and not given_up) or any(
                record.round_number == item.sequence for record in self.state.search.rounds
            ):
                return
            outcome = (
                implementation.outcome
                if implementation is not None
                else HypothesisOutcome.IMPLEMENTATION_FAILED
            )
            evaluation = item.evaluation
            review = item.review
            accepted = evaluation.accepted if evaluation is not None else None
            metrics = dict(evaluation.metrics) if evaluation is not None else {}
            framework_metric = (
                evaluation is not None
                and evaluation.metric_name is not None
                and evaluation.metric_value is not None
            )
            baseline = (
                hypothesis_transitions.metric_baseline(
                    parent_round=None,
                    parent_commit=item.parent_revision,
                    metric=evaluation.metric_name,
                    rounds=self.state.search.rounds,
                )
                if framework_metric
                else None
            )
            baseline_value = (
                hypothesis_transitions.record_metric_value(baseline, evaluation.metric_name)
                if baseline is not None and evaluation is not None
                else None
            )
            direction = (
                evaluation.metric_direction.value
                if framework_metric and evaluation.metric_direction is not None
                else None
            )
            comparison = (
                self.state.search.metrics.compare(
                    Measurement(
                        metric=evaluation.metric_name,
                        value=evaluation.metric_value,
                        direction=direction,
                    ),
                    Measurement(
                        metric=evaluation.metric_name,
                        value=baseline_value,
                        direction=direction,
                    )
                    if baseline_value is not None
                    else None,
                )
                if framework_metric
                else None
            )
            disposition, retained = self._candidate_decision(
                accepted=accepted,
                metrics=metrics,
                headline=(
                    Measurement(
                        metric=evaluation.metric_name,
                        value=evaluation.metric_value,
                        direction=direction,
                    )
                    if framework_metric
                    else None
                ),
            )
            record = RoundRecord(
                round_number=item.sequence,
                commit=item.candidate_revision,
                perf_metric=evaluation.metric_value if framework_metric else None,
                perf_unit=evaluation.metric_name if framework_metric else None,
                passed=review.passed if review is not None else not given_up,
                reviewed=review is not None,
                hypothesis_id=item.hypothesis_id,
                hypothesis_declared_outcome=outcome.value,
                judge_verdict=(
                    "pass" if review and review.passed else "fail" if review else "deferred"
                ),
                hypothesis_outcome=outcome.value,
                hypothesis_claim=item.plan.hypothesis,
                hypothesis_task=item.plan.task,
                hypothesis_parent_commit=item.parent_revision,
                metrics=metrics,
                official_evaluation=evaluation is not None,
                official_evaluation_reason=(
                    "dynamic_promotion" if evaluation is not None else None
                ),
                candidate_disposition=disposition.value,
                candidate_metrics=metrics,
                candidate_retained=retained,
                perf_direction=direction,
                perf_baseline_round=baseline.round_number if baseline is not None else None,
                perf_baseline_commit=baseline.commit if baseline is not None else None,
                perf_baseline_metric=baseline_value,
                perf_delta_pct=(
                    (evaluation.metric_value - baseline_value) / abs(baseline_value) * 100
                    if framework_metric and baseline_value not in {None, 0}
                    else None
                ),
                perf_comparison=comparison,
                perf_provenance="framework" if framework_metric else None,
                attempts=item.budget.spent,
            )
            active_search = self.state.search.model_copy(
                update={"active_hypothesis_id": item.hypothesis_id},
                deep=True,
            )
            self.state.search = hypothesis_transitions.append_round(
                active_search,
                record,
                keep_active=outcome is HypothesisOutcome.CONTINUE,
            )
            if self.state.search.active_hypothesis_id is not None:
                self.state.search = hypothesis_transitions.finish_hypothesis(self.state.search)
            await self._commit(f"dynamic: record hypothesis {item.hypothesis_id}")

    def winner(self) -> DynamicWorkstream | None:
        """Return the workstream of the best recorded round that beats the input, if any."""
        search = HypothesisSearch(hypothesis_config(self.options))
        # Filter again here: a round recorded before the input baseline was
        # measured was not gated by it.
        winner = search.best(
            [
                record
                for record in self.state.search.rounds
                if self._gate.admits(
                    dict(record.metrics),
                    hypothesis_transitions.headline_measurement(record),
                )
            ],
            space=self.options.metric_space,
        )
        if winner is None:
            return None
        return next(
            (item for item in self.state.workstreams if item.sequence == winner.round_number),
            None,
        )

    def _candidate_decision(
        self,
        *,
        accepted: bool | None,
        metrics: dict[str, float],
        headline: Measurement | None,
    ) -> tuple[CandidateDisposition, bool | None]:
        """Apply the shared noise aware frontier policy to one evaluated candidate."""
        if accepted is False:
            return CandidateDisposition.DISCARD, False
        if accepted is not True:
            return CandidateDisposition.UNASSESSED, None
        space = self.options.metric_space
        comparable = space.complete(metrics) if space.objectives else headline is not None
        if not comparable:
            return CandidateDisposition.UNASSESSED, None
        if not self._gate.admits(metrics, headline):
            return CandidateDisposition.DISCARD, False
        search = HypothesisSearch(hypothesis_config(self.options))
        conflict = search.pareto_conflict(
            disposition=CandidateDisposition.PARETO_FRONTIER,
            metrics=metrics,
            records=self.state.search.rounds,
            space=space,
        )
        if conflict is not None:
            return CandidateDisposition.DISCARD, False
        return CandidateDisposition.PARETO_FRONTIER, True

    def _history_projection(self, live: Mapping[str, Sequence[AgentEvaluation]]) -> str:
        rows = [
            self.history_row(item, live=live.get(item.hypothesis_id))
            for item in self.state.workstreams[-_MAX_HISTORY_ROWS:]
        ]
        return json.dumps(rows, separators=(",", ":"))

    def history_row(
        self, item: DynamicWorkstream, *, live: Sequence[AgentEvaluation] | None = None
    ) -> dict[str, object]:
        """Project one workstream with the disposition its recorded round received.

        An accepted candidate can still be discarded (it did not beat the input
        or was dominated); without the disposition it reads as a success. A
        strategy update (park, abandon) shows as ``strategy``. While an
        implementer turn runs (``live`` is given), the facts of the attempt
        before it move under ``previous_attempt`` and ``running_evaluations``
        lists what the running turn has measured so far.
        """
        record = next(
            (record for record in self.state.search.rounds if record.round_number == item.sequence),
            None,
        )
        hypothesis = next(
            (
                entry
                for entry in self.state.search.hypotheses
                if entry.hypothesis_id == item.hypothesis_id
            ),
            None,
        )
        attempt = _attempt_row(item)
        strategy = {
            "strategy": hypothesis.strategy.value if hypothesis is not None else None,
            "strategy_reason": (
                _bounded_optional(hypothesis.strategy_reason, _MAX_HISTORY_REVIEW_CHARS)
                if hypothesis is not None
                else None
            ),
            "disposition": record.candidate_disposition if record is not None else None,
        }
        if live is None:
            return {**_identity_row(item), **attempt, **strategy}
        return {
            **_identity_row(item),
            "running_evaluations": [
                _compact_agent_evaluation(entry) for entry in live[-_MAX_LIVE_EVALUATIONS:]
            ],
            "previous_attempt": (
                attempt if item.implementation is not None or item.last_error is not None else None
            ),
            **strategy,
        }


def _buildable_row(item: DynamicWorkstream) -> dict[str, object]:
    """Project one buildable candidate with its trusted measurement."""
    evaluation = item.evaluation
    return {
        "hypothesis_id": item.hypothesis_id,
        "title": normalize_hypothesis_title(item.plan.title),
        "revision": _bounded_optional(item.candidate_revision, _MAX_HISTORY_REVISION_CHARS),
        "benchmark_passed": evaluation.benchmark_passed if evaluation is not None else None,
        "metric_name": (
            _bounded_optional(evaluation.metric_name, _MAX_HISTORY_METRIC_NAME_CHARS)
            if evaluation is not None
            else None
        ),
        "metric_value": evaluation.metric_value if evaluation is not None else None,
        "metric_unit": (
            _bounded_optional(evaluation.metric_unit, _MAX_HISTORY_METRIC_UNIT_CHARS)
            if evaluation is not None
            else None
        ),
    }


def _identity_row(item: DynamicWorkstream) -> dict[str, object]:
    return {
        "hypothesis_id": item.hypothesis_id,
        "title": normalize_hypothesis_title(item.plan.title),
        "phase": item.phase.value,
    }


def _compact_agent_evaluation(evaluation: AgentEvaluation) -> dict[str, object]:
    """Project one agent-submitted evaluation: each finished stage's verdict and metrics."""
    return {
        "revision": _bounded_optional(evaluation.revision, _MAX_HISTORY_REVISION_CHARS),
        "status": evaluation.status.value,
        "stages": [
            {
                "kind": stage.kind,
                "outcome": stage.outcome.value,
                "metrics": [
                    {
                        "name": metric.name[:_MAX_HISTORY_METRIC_NAME_CHARS],
                        "value": metric.value,
                        "unit": _bounded_optional(metric.unit, _MAX_HISTORY_METRIC_UNIT_CHARS),
                    }
                    for metric in stage.metrics[:_MAX_HISTORY_METRICS]
                ],
            }
            for stage in evaluation.stages
        ],
        # A failure states its cause last.
        "failure_tail": (
            evaluation.failure[-_MAX_LIVE_FAILURE_CHARS:]
            if evaluation.failure is not None
            else None
        ),
    }


def _attempt_row(item: DynamicWorkstream) -> dict[str, object]:
    """Project one workstream's latest attempt as bounded decision facts."""
    return {
        "outcome": item.implementation.outcome.value if item.implementation is not None else None,
        "summary": _bounded_optional(
            item.implementation.summary
            if item.implementation is not None
            # Without the error the planner sees a bare ``failed`` and
            # invents a cause for it.
            else f"Attempt failed before any result: {item.last_error}"
            if item.last_error is not None
            else None,
            _MAX_HISTORY_SUMMARY_CHARS,
        ),
        "next_step": (
            _bounded_optional(item.implementation.next_step, _MAX_HISTORY_NEXT_STEP_CHARS)
            if item.implementation is not None
            else ""
        ),
        "review": _compact_review(item.review),
        "revision": _bounded_optional(
            item.candidate_revision,
            _MAX_HISTORY_REVISION_CHARS,
        ),
        "evidence": [ref.model_dump(mode="json") for ref in _evidence(item)],
        "evaluation": _compact_evaluation(item.evaluation),
    }


def _evidence(workstream: DynamicWorkstream) -> tuple[EvidenceReference, ...]:
    if workstream.implementation is not None:
        return workstream.implementation.evidence
    return workstream.plan.evidence


def _compact_review(review: ReviewResult | None) -> dict[str, object] | None:
    """Project the verdict and its reason so the planner can avoid a rejected path."""
    if review is None:
        return None
    reason = review.feedback or review.analysis
    return {
        "passed": review.passed,
        "reason": _bounded_optional(reason, _MAX_HISTORY_REVIEW_CHARS),
    }


def _compact_evaluation(result: EvaluationResult | None) -> dict[str, object] | None:
    """Project bounded decision facts without copying command output into prompts."""
    if result is None:
        return None
    metric_names = sorted(result.metrics)[:_MAX_HISTORY_METRICS]
    return {
        "accepted": result.accepted,
        "local_validation_passed": result.local_validation_passed,
        "accuracy_passed": result.accuracy_passed,
        "benchmark_passed": result.benchmark_passed,
        "metric_name": _bounded_optional(result.metric_name, _MAX_HISTORY_METRIC_NAME_CHARS),
        "metric_value": result.metric_value,
        "metric_direction": (
            result.metric_direction.value if result.metric_direction is not None else None
        ),
        "metric_unit": _bounded_optional(result.metric_unit, _MAX_HISTORY_METRIC_UNIT_CHARS),
        "metrics": {
            name[:_MAX_HISTORY_METRIC_NAME_CHARS]: result.metrics[name] for name in metric_names
        },
    }


def _bounded_optional(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit]


def hypothesis_config(options: DynamicOptions) -> HypothesisConfig:
    """Return the shared search configuration: one round per workstream."""
    return HypothesisConfig(
        max_rounds=options.max_rounds * options.max_in_flight,
        judge_every=options.judge_every,
        max_retries_per_round=options.max_retries_per_round,
    )


__all__ = ["Rounds", "hypothesis_config"]
