"""Recording workstream results as rounds, and what the planner sees of them."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING

from vibesys.hypothesis import (
    CandidateDisposition,
    HypothesisConfig,
    HypothesisOutcome,
    HypothesisSearch,
    RoundRecord,
    normalize_hypothesis_title,
)
from vibesys.hypothesis import transitions as hypothesis_transitions
from vibesys.metrics import Measurement
from vibesys.orchestration.dynamic import models as dynamic_models
from vibesys.orchestration.dynamic import steers
from vibesys.orchestration.dynamic.lifecycle import CompleteIntent, step, withdrawing
from vibesys.orchestration.dynamic.models import (
    BenchmarkGap,
    BuildableCandidate,
    DynamicWorkstream,
    HypothesisTrend,
    InputNotMeasurable,
    MeasuredIteration,
    PortfolioView,
    WorkstreamPhase,
)
from vibesys.orchestration.dynamic.parents.api import ParentCatalog
from vibesys.orchestration.dynamic.parents.api import options as parent_options
from vibesys.orchestration.dynamic.prompts import render_steer_dropped
from vibesys.orchestration.dynamic.transitions import SettlementProposed
from vibesys.orchestration.dynamic.transitions import step as envelope_step
from vs_evaluation.api import EvidenceOutcome
from vs_runtime.api import MetricDirection

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from vibesys.orchestration.dynamic.input_gate import InputGate
    from vibesys.orchestration.dynamic.models import (
        DynamicOptions,
        DynamicProfile,
        DynamicState,
        EvaluationResult,
        EvidenceReference,
        ReviewResult,
        VerifiedCandidate,
    )
    from vs_runtime.api import AgentEvaluation, PartialMeasurement

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
# A profile's diagnosis is the fact the planner scheduled it for, so it keeps
# more text than an attempt summary.
_MAX_PROFILE_QUESTION_CHARS = 600
_MAX_PROFILE_DIAGNOSIS_CHARS = 2000
_MAX_PROFILE_COMPONENTS = 8
_MAX_PROFILE_EVIDENCE = 8


@dataclass(slots=True)
class Rounds:
    """Records each finished workstream as a round and selects the winner.

    Every candidate decision is made against the input reading of ``gate``;
    the winner is re-filtered at selection because a round recorded before
    the input was measured was not gated. The planner's view of the history
    (bounded rows and the input reading) is projected here too. ``clock``
    returns run-elapsed seconds; settlement that drops steers requires it.
    Read-only portfolio projections can omit the clock.
    """

    options: DynamicOptions
    state: DynamicState
    gate: InputGate
    lock: asyncio.Lock
    commit: Callable[[str], Awaitable[None]]
    clock: Callable[[], float] | None = None
    parents: ParentCatalog = dataclass_field(default_factory=ParentCatalog)

    def _now(self) -> float:
        if self.clock is None:
            message = "dynamic settlement requires an injected clock"
            raise ValueError(message)
        return self.clock()

    def planner_context(
        self,
        live: Mapping[str, Sequence[AgentEvaluation]] | None = None,
        buildable: Sequence[BuildableCandidate] = (),
    ) -> dict[str, object]:
        """Return the history and input facts every planning prompt states.

        ``live`` maps each running implementer turn to the evaluations it has
        submitted so far; its row shows them in place of the facts of the
        attempt before it. ``buildable`` lists the candidates a new workstream
        may name as its parent (see :meth:`buildable`).
        """
        baseline = self.state.baseline
        return {
            "baseline": (
                json.dumps(_compact_evaluation(baseline), separators=(",", ":"))
                if baseline is not None and baseline.benchmark_passed
                else ""
            ),
            "input_failure": (
                InputNotMeasurable(
                    reason=(baseline.benchmark_feedback or "no feedback provided")[
                        :_MAX_HISTORY_REVIEW_CHARS
                    ]
                )
                if baseline is not None and not baseline.benchmark_passed
                else None
            ),
            "input_partial": (
                json.dumps(_partial_row(baseline.partial_measurement), separators=(",", ":"))
                if baseline is not None
                and not baseline.benchmark_passed
                and baseline.partial_measurement is not None
                else ""
            ),
            "portfolio_view": self.portfolio_view(),
            "history": self._history_projection(live or {}),
            "buildable": json.dumps(
                [_buildable_row(item) for item in buildable], separators=(",", ":")
            ),
            # Only a hypothesis can be continued, so older profiles are not listed.
            "older_ids": ", ".join(
                item.hypothesis_id
                for item in self._history_entries()[:-_MAX_HISTORY_ROWS]
                if isinstance(item, DynamicWorkstream)
            ),
        }

    def measured_iterations(self, item: DynamicWorkstream) -> tuple[MeasuredIteration, ...]:
        """Return retained measurements plus the latest trusted candidate measurement.

        Continuations retain these minimal facts before replacing the workstream.
        Accuracy failures contribute no benchmark evidence. Duplicate framework
        and implementer evaluations of the same revision contribute once. An
        inherited verified candidate keeps its original observation sequence.
        """
        rows = list(item.measured_iterations)
        if item.verified is not None:
            verified_rows = _measured_rows(item.verified, item.hypothesis_id, item.sequence)
            sequence = item.verified.observation_sequence
            if sequence is None:
                # Compatibility for saved candidates predating observation provenance.
                sequence = next(
                    (
                        row.sequence
                        for row in reversed(rows)
                        if row.model_copy(update={"sequence": item.sequence}) in verified_rows
                    ),
                    item.sequence,
                )
            rows.extend(row.model_copy(update={"sequence": sequence}) for row in verified_rows)
        if item.evaluation is not None and item.evaluation.accuracy_passed is not False:
            rows.extend(_measured_rows(item.evaluation, item.hypothesis_id, item.sequence))
        return tuple({(row.sequence, row.revision, row.name): row for row in rows}.values())

    def portfolio_view(self) -> PortfolioView:
        """Derive gaps and full lineage trends without the history-row truncation.

        Compare only matching quantity names, units, and directions. A warmup
        quantity and a headline quantity remain distinct even with the same unit.
        """
        measurements = [
            row for item in self.state.workstreams for row in self.measured_iterations(item)
        ]
        # Generic round records preserve older headline measurements, including
        # runs written before measured_iterations existed. Partial values need
        # the typed continuation history because they are not headline metrics.
        recorded = {(row.sequence, row.name) for row in measurements}
        accuracy_failures = {
            item.sequence
            for item in self.state.workstreams
            if item.evaluation is not None and item.evaluation.accuracy_passed is False
        }
        measurements.extend(
            MeasuredIteration(
                hypothesis_id=record.hypothesis_id,
                sequence=record.round_number,
                revision=record.commit,
                name=record.perf_unit,
                value=record.perf_metric,
                direction=MetricDirection(record.perf_direction),
            )
            for record in self.state.search.rounds
            if record.perf_metric is not None
            and record.perf_unit is not None
            and record.perf_direction is not None
            and record.hypothesis_id is not None
            and record.commit is not None
            and record.round_number not in accuracy_failures
            and (record.round_number, record.perf_unit) not in recorded
        )
        baseline = self.state.baseline
        if baseline is not None:
            measurements.extend(_measured_rows(baseline, None, 0))
        measurements.sort(key=lambda row: row.sequence)
        parents = {
            item.hypothesis_id: item.lineage_parent_id or item.plan.parent_hypothesis_id
            for item in self.state.workstreams
        }
        trends = tuple(
            HypothesisTrend(
                hypothesis_id=identifier,
                iterations=tuple(
                    row
                    for row in measurements
                    if row.hypothesis_id is not None
                    and _in_lineage(row.hypothesis_id, identifier, parents)
                ),
            )
            for identifier in parents
        )
        return PortfolioView(gaps=_benchmark_gaps(measurements), trends=trends)

    def _history_entries(self) -> list[DynamicWorkstream | DynamicProfile]:
        """Return every scheduled workstream, implement or profile, in schedule order."""
        return sorted(
            [*self.state.workstreams, *self.state.profiles], key=lambda item: item.sequence
        )

    def buildable(self) -> tuple[BuildableCandidate, ...]:
        """Project exact retained accuracy-verified snapshots as sibling parents.

        Active and cancelled producers remain eligible once independent retention
        and canonical accuracy receipts exist. The catalog owns comparable groups,
        best observed partials and original latest chronology; unlike contexts have
        no scientific total order. Existing framework/verified latest selection is
        retained as the legacy selector path. The run shell checks reproducibility
        before offering or materializing any revision. Fitness grants no adoption.
        """
        candidates: list[BuildableCandidate] = []
        offered = parent_options(self.parents)
        for item in self.state.workstreams:
            snapshots = tuple(
                option for option in offered if option.snapshot.hypothesis_id == item.hypothesis_id
            )
            for option in snapshots:
                snapshot = option.snapshot
                benchmark = snapshot.benchmark
                metric = next(iter(benchmark.metrics), None) if benchmark is not None else None
                candidates.append(
                    BuildableCandidate(
                        hypothesis_id=item.hypothesis_id,
                        title=normalize_hypothesis_title(item.plan.title),
                        revision=snapshot.revision,
                        content_digest=snapshot.content_digest,
                        benchmark_passed=(
                            benchmark.outcome is EvidenceOutcome.PASSED
                            if benchmark is not None
                            else None
                        ),
                        metric_name=metric.name if metric is not None else None,
                        metric_value=metric.value if metric is not None else None,
                        metric_unit=metric.unit if metric is not None else None,
                        metric_direction=(
                            MetricDirection(metric.direction)
                            if metric is not None and metric.direction is not None
                            else None
                        ),
                        partial_measurement=benchmark.partial_measurement
                        if benchmark is not None
                        else None,
                        option_id=option.option_id,
                        handle_id=snapshot.handle_id,
                        submission_index=snapshot.submission_index,
                        latest_verified=option.latest_verified,
                        best_partial=option.best_partial,
                        active=item.phase is WorkstreamPhase.IMPLEMENTING,
                        comparison_key=option.comparison_key,
                        change_summary=snapshot.change_summary,
                        artifact_refs=tuple(
                            artifact.path
                            for receipt in (snapshot.accuracy, snapshot.benchmark)
                            if receipt is not None
                            for artifact in receipt.artifacts
                        ),
                        generation=snapshot.generation,
                        accuracy_evidence_id=snapshot.accuracy.evidence_id,
                        benchmark_evidence_id=benchmark.evidence_id
                        if benchmark is not None
                        else None,
                        complete_metrics=benchmark.metrics
                        if benchmark is not None and benchmark.outcome is EvidenceOutcome.PASSED
                        else (),
                        benchmark_failure=benchmark.semantic_summary
                        if benchmark is not None and benchmark.outcome is EvidenceOutcome.FAILED
                        else None,
                        legacy_default=False,
                    )
                )
            if item.phase in {
                WorkstreamPhase.IMPLEMENTING,
                WorkstreamPhase.CANCELLED,
            } or withdrawing(self.state.lifecycle, item.hypothesis_id):
                continue
            evaluation = item.evaluation
            if (
                evaluation is not None
                and evaluation.accuracy_passed is True
                and item.candidate_revision is not None
                and evaluation.revision == item.candidate_revision
            ):
                candidates.append(
                    BuildableCandidate(
                        hypothesis_id=item.hypothesis_id,
                        title=normalize_hypothesis_title(item.plan.title),
                        revision=item.candidate_revision,
                        content_digest=None,
                        benchmark_passed=evaluation.benchmark_passed,
                        metric_name=evaluation.metric_name,
                        metric_value=evaluation.metric_value,
                        metric_unit=evaluation.metric_unit,
                        metric_direction=evaluation.metric_direction,
                        partial_measurement=evaluation.partial_measurement,
                    )
                )
            elif item.verified is not None and any(
                snapshot.revision == item.verified.revision
                and snapshot.content_digest == item.verified.content_digest
                for snapshot in self.parents.snapshots
                if snapshot.hypothesis_id == item.hypothesis_id
            ):
                verified = item.verified
                candidates.append(
                    BuildableCandidate(
                        hypothesis_id=item.hypothesis_id,
                        title=normalize_hypothesis_title(item.plan.title),
                        revision=verified.revision,
                        content_digest=verified.content_digest,
                        benchmark_passed=verified.benchmark_passed,
                        metric_name=verified.metric_name,
                        metric_value=verified.metric_value,
                        metric_unit=verified.metric_unit,
                        metric_direction=verified.metric_direction,
                        partial_measurement=verified.partial_measurement,
                    )
                )
        legacy = {item.hypothesis_id: item.revision for item in candidates if item.legacy_default}
        for candidate in candidates:
            if candidate.latest_verified and candidate.hypothesis_id not in legacy:
                legacy[candidate.hypothesis_id] = candidate.revision
        unique: dict[tuple[str, str], BuildableCandidate] = {}
        for candidate in candidates:
            key = (candidate.hypothesis_id, candidate.revision)
            if key not in unique or candidate.option_id is not None:
                unique[key] = candidate
        canonical_order = {option.option_id: position for position, option in enumerate(offered)}
        ordered = sorted(
            unique.values(),
            key=lambda candidate: canonical_order.get(candidate.option_id, len(offered)),
        )
        return tuple(
            candidate.model_copy(
                update={"legacy_default": legacy.get(candidate.hypothesis_id) == candidate.revision}
            )
            for candidate in ordered
        )

    async def cancel(self, index: int, operation_id: str) -> None:
        """Commit cancellation, discarded round, steer drops and intent completion together."""
        await self.record(index, cancellation_id=operation_id)

    async def record(self, index: int, *, cancellation_id: str | None = None) -> None:
        """Commit one workstream result through shared hypothesis transitions.

        Recording the round settles the workstream: every path that ends one
        (finished, failed, or given up) passes here. Steers still pending for
        it are dropped and journaled in the same commit, since no worker turn
        of the workstream follows.
        """
        if cancellation_id is None and self.state.workstreams[index].evaluation is not None:
            # The candidate decision compares against the input measurement.
            await self.gate.measured()
        async with self.lock:
            item = self.state.workstreams[index]
            if cancellation_id is None and withdrawing(self.state.lifecycle, item.hypothesis_id):
                return
            if cancellation_id is not None and any(
                record.round_number == item.sequence for record in self.state.search.rounds
            ):
                self.state.lifecycle, _ = step(
                    self.state.lifecycle, CompleteIntent(operation_id=cancellation_id)
                )
                await self.commit(f"dynamic: {item.hypothesis_id} already settled")
                return
            implementation = item.implementation
            # A slot given up before any implementer turn returned still ends
            # its hypothesis: without a round the hypothesis stays incomplete,
            # and the planner, told the slot failed, could not abandon it.
            # A cancelled one ends it the same way.
            cancelled = cancellation_id is not None or item.phase is WorkstreamPhase.CANCELLED
            given_up = implementation is None and (
                item.phase is WorkstreamPhase.FAILED or cancelled
            )
            if (implementation is None and not given_up) or any(
                record.round_number == item.sequence for record in self.state.search.rounds
            ):
                return
            outcome = (
                HypothesisOutcome.INCONCLUSIVE
                if cancelled
                else implementation.outcome
                if implementation is not None
                else HypothesisOutcome.IMPLEMENTATION_FAILED
            )
            evaluation = item.evaluation
            review = item.review
            accepted = evaluation.accepted if evaluation is not None else None
            metrics = (
                dict(evaluation.metrics)
                if evaluation is not None and evaluation.accuracy_passed is not False
                else {}
            )
            framework_metric = (
                evaluation is not None
                and evaluation.accuracy_passed is not False
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
            if cancelled:
                disposition, retained = CandidateDisposition.DISCARD, False
            record = RoundRecord(
                round_number=item.sequence,
                commit=item.candidate_revision,
                perf_metric=evaluation.metric_value if framework_metric else None,
                perf_unit=evaluation.metric_name if framework_metric else None,
                passed=False
                if cancelled
                else review.passed
                if review is not None
                else not given_up,
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
            if cancellation_id is not None:
                at_s = self._now()
                reduced, _ = envelope_step(
                    self.state,
                    SettlementProposed(
                        operation_id=cancellation_id,
                        record=record,
                        drop_journal=tuple(
                            dynamic_models.JournalEntry(
                                at_s=at_s,
                                turn=self.state.agent.turns,
                                kind="steer",
                                subject=item.hypothesis_id,
                                text=render_steer_dropped(
                                    note_sha256=note.note_sha256,
                                    sent_at_s=note.sent_at_s,
                                ),
                            )
                            for note in steers.pending(self.state, item.hypothesis_id)
                        )
                        if self.state.agent is not None
                        else (),
                        at_s=at_s,
                        retry_limit=self.options.max_retries_per_round,
                    ),
                )
                self.state.workstreams = reduced.workstreams
                self.state.search = reduced.search
                self.state.agent = reduced.agent
                self.state.lifecycle = reduced.lifecycle
            else:
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
                if steers.pending(self.state, item.hypothesis_id):
                    steers.drop_pending(self.state, item.hypothesis_id, at_s=self._now())
            await self.commit(f"dynamic: record hypothesis {item.hypothesis_id}")

    def winner(self) -> DynamicWorkstream | None:
        """Return the workstream of the best recorded round that beats the input, if any."""
        search = HypothesisSearch(hypothesis_config(self.options))
        # Filter again here: a round recorded before the input baseline was
        # measured was not gated by it.
        winner = search.best(
            [
                record
                for record in self.state.search.rounds
                if not any(
                    item.sequence == record.round_number
                    and (
                        item.phase is WorkstreamPhase.CANCELLED
                        or withdrawing(self.state.lifecycle, item.hypothesis_id)
                    )
                    for item in self.state.workstreams
                )
                and self.gate.admits(
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
        if not self.gate.admits(metrics, headline):
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
            if isinstance(item, DynamicWorkstream)
            else _profile_row(item)
            for item in self._history_entries()[-_MAX_HISTORY_ROWS:]
        ]
        return json.dumps(rows, separators=(",", ":"))

    def history_row(
        self, item: DynamicWorkstream, *, live: Sequence[AgentEvaluation] | None = None
    ) -> dict[str, object]:
        """Project one workstream with the disposition its recorded round received.

        An accepted candidate can still be discarded (it did not beat the input
        or was dominated); without the disposition it reads as a success. A
        strategy update (park, abandon) shows as ``strategy``. While an
        implementer turn runs (``live`` is given, or the phase is
        ``implementing``), the facts of the attempt before it move under
        ``previous_attempt`` and ``running_evaluations`` lists what the
        running turn has measured so far.
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
            "strategy_reason_kind": item.strategy_reason_kind,
            "strategy_reason": (
                _bounded_optional(hypothesis.strategy_reason, _MAX_HISTORY_REVIEW_CHARS)
                if hypothesis is not None
                else None
            ),
            "disposition": record.candidate_disposition if record is not None else None,
        }
        if live is None and item.phase is not WorkstreamPhase.IMPLEMENTING:
            return {**_identity_row(item), **attempt, **strategy}
        # An implementing workstream's retained result belongs to the attempt
        # before the current one, also while a resumed turn has not started.
        return {
            **_identity_row(item),
            "running_evaluations": [
                _compact_agent_evaluation(entry) for entry in (live or ())[-_MAX_LIVE_EVALUATIONS:]
            ],
            "previous_attempt": (
                attempt if item.implementation is not None or item.last_error is not None else None
            ),
            **strategy,
        }


def _measured_rows(
    result: EvaluationResult | VerifiedCandidate,
    identifier: str | None,
    sequence: int,
) -> tuple[MeasuredIteration, ...]:
    partial = result.partial_measurement
    common = {"hypothesis_id": identifier, "sequence": sequence, "revision": result.revision}
    rows = []
    if partial is not None:
        rows.append(
            MeasuredIteration.model_validate({**common, **partial.model_dump(exclude={"progress"})})
        )
    if (
        result.metric_name is not None
        and result.metric_value is not None
        and result.metric_direction is not None
    ):
        rows.append(
            MeasuredIteration(
                hypothesis_id=identifier,
                sequence=sequence,
                revision=result.revision,
                name=result.metric_name,
                value=result.metric_value,
                direction=result.metric_direction,
                unit=result.metric_unit,
            )
        )
    return tuple(rows)


def _in_lineage(identifier: str, ancestor: str, parents: Mapping[str, str | None]) -> bool:
    seen = set()
    current: str | None = identifier
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def _benchmark_gaps(measurements: Sequence[MeasuredIteration]) -> tuple[BenchmarkGap, ...]:
    groups: dict[tuple[str, str | None, MetricDirection], list[MeasuredIteration]] = {}
    for row in measurements:
        groups.setdefault((row.name, row.unit, row.direction), []).append(row)
    gaps = []
    for (name, unit, direction), rows in groups.items():
        targets = {row.target for row in rows if row.target is not None}
        # A changed target is a distinct gate; never pretend a stale bar is current.
        for target in sorted(targets):
            values = [row.value for row in rows if row.target in {None, target}]
            best = max(values) if direction is MetricDirection.MAXIMIZE else min(values)
            numerator, denominator = (
                (target, best) if direction is MetricDirection.MAXIMIZE else (best, target)
            )
            ratio = numerator / denominator if numerator > 0 and denominator > 0 else None
            if ratio is not None and not math.isfinite(ratio):
                ratio = None
            gaps.append(
                BenchmarkGap(
                    name=name,
                    unit=unit,
                    direction=direction,
                    best_value=best,
                    required_value=target,
                    required_ratio=ratio,
                )
            )
    return tuple(gaps)


def _profile_row(item: DynamicProfile) -> dict[str, object]:
    """Project one profile workstream: its target, question, and trusted outcome."""
    outcome = item.outcome
    return {
        "kind": "profile",
        "profile_id": item.profile_id,
        "target_hypothesis_id": item.plan.target_hypothesis_id,
        "revision": item.revision,
        "question": _bounded_optional(item.plan.question, _MAX_PROFILE_QUESTION_CHARS),
        # A profile without an outcome is running (or resumes at the next start).
        "status": outcome.status.value if outcome is not None else "running",
        "operation_id": outcome.operation_id if outcome is not None else None,
        "missing_fields": [field.value for field in outcome.missing_fields]
        if outcome is not None
        else [],
        "diagnosis": _bounded_optional(
            outcome.diagnosis if outcome is not None else None, _MAX_PROFILE_DIAGNOSIS_CHARS
        ),
        "components": [
            {"name": component.name[:_MAX_HISTORY_METRIC_NAME_CHARS], "share": component.share}
            for component in (outcome.components if outcome is not None else ())[
                :_MAX_PROFILE_COMPONENTS
            ]
        ],
        "evidence_ids": (
            list(outcome.evidence_ids[:_MAX_PROFILE_EVIDENCE]) if outcome is not None else []
        ),
        # A failure states its cause last.
        "failure_tail": (
            outcome.failure[-_MAX_LIVE_FAILURE_CHARS:]
            if outcome is not None and outcome.failure is not None
            else None
        ),
    }


def _buildable_row(item: BuildableCandidate) -> dict[str, object]:
    """Project one buildable candidate with its trusted measurement."""
    return {
        "hypothesis_id": item.hypothesis_id,
        "title": item.title,
        "revision": item.revision,
        "content_digest": item.content_digest,
        "option_id": item.option_id,
        "handle_id": item.handle_id,
        "submission_index": item.submission_index,
        "latest_verified": item.latest_verified,
        "best_partial": item.best_partial,
        "active": item.active,
        "comparison_key": item.comparison_key.model_dump(mode="json")
        if item.comparison_key is not None
        else None,
        "change_summary": item.change_summary,
        "artifact_refs": item.artifact_refs,
        "legacy_default": item.legacy_default,
        "generation": item.generation,
        "accuracy_passed": True,
        "accuracy_evidence_id": item.accuracy_evidence_id,
        "benchmark_evidence_id": item.benchmark_evidence_id,
        "benchmark_failure": item.benchmark_failure,
        "retained": True,
        "complete_metrics": [metric.model_dump(mode="json") for metric in item.complete_metrics],
        "benchmark_passed": item.benchmark_passed,
        "metric_name": _bounded_optional(item.metric_name, _MAX_HISTORY_METRIC_NAME_CHARS),
        "metric_value": item.metric_value,
        "metric_unit": _bounded_optional(item.metric_unit, _MAX_HISTORY_METRIC_UNIT_CHARS),
        "partial_measurement": _partial_row(item.partial_measurement),
    }


def _partial_row(partial: PartialMeasurement | None) -> dict[str, object] | None:
    """Project a partial measurement with its free-text fields bounded."""
    if partial is None:
        return None
    return {
        **partial.model_dump(mode="json", exclude_none=True),
        "name": partial.name[:_MAX_HISTORY_METRIC_NAME_CHARS],
        **(
            {"unit": partial.unit[:_MAX_HISTORY_METRIC_UNIT_CHARS]}
            if partial.unit is not None
            else {}
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
                "partial_measurement": _partial_row(stage.partial_measurement),
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
        "partial_measurement": _partial_row(result.partial_measurement),
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


__all__ = ["BuildableCandidate", "Rounds", "hypothesis_config"]
