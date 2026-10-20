"""What the planning call reads of the search: budgets, offered parents and the compact history.

`PlanWorld` is a read-only slice of the search's record, built for one planning ask. Everything
on it is a pure function of that slice; the prompt it builds is a typed value, never text.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from vibesys.candidates.api import Baseline, failure, measurement, reading
from vibesys.metrics import MetricComparison, MetricRow, MetricSpace, reading_of
from vibesys.orchestration.dynamic.models import ProfileOffer
from vibesys.orchestration.dynamic.search import _briefs as briefs
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._main import MergeOptions
from vibesys.orchestration.dynamic.search._parents import ParentOption, ParentSnapshot, options
from vibesys.orchestration.dynamic.search._plan import PlanLedger
from vibesys.orchestration.dynamic.search._prompts import (
    BuildableRow,
    CompositeRow,
    EvaluationQueueView,
    HistoryRow,
    InputOverdueView,
    PartsVerdict,
    PlannerCorrection,
    PlannerPrompt,
    ProfileHistoryRow,
    ProfileHistoryStatus,
    WorkstreamHistoryRow,
)
from vibesys.orchestration.dynamic.search._values import (
    CombineSource,
    FinishedWork,
    HypothesisRecord,
    MainMeasurement,
    MergeRecord,
    PlanProblem,
    WorkKind,
    WorkPlan,
)
from vs_core.api.search import EvidenceKind, InvocationRef, RevisionRef


class RunningWork(Protocol):
    """What the planner is told of a workstream that has not finished."""

    @property
    def key(self) -> str:
        """The workstream's key."""
        ...

    @property
    def plan(self) -> WorkPlan:
        """What the workstream was scheduled to do."""
        ...

    @property
    def sequence(self) -> int:
        """Its position in the order workstreams were scheduled."""
        ...

    @property
    def parent(self) -> RevisionRef:
        """The revision it builds on."""
        ...


_HISTORY_ROWS = 20
_HISTORY_TITLE_CHARS = 200
_HISTORY_SUMMARY_CHARS = 600
_PROFILE_EVIDENCE_IDS = 8


def remaining(config: DynamicConfig, *, started: int, refunded: int) -> int:
    """Workstreams the run may still schedule; a refund returns its slot to the budget."""
    return max(config.start_budget - started + refunded, 0)


def free_slots(config: DynamicConfig, *, active: int, left: int) -> int:
    """Free slots the next plan may fill, bounded by the remaining budget."""
    return max(min(config.max_in_flight - active, left), 0)


def offered_parents(
    parents: Iterable[ParentSnapshot], withheld: tuple[str, ...]
) -> tuple[ParentOption, ...]:
    """Parents the planner may name: proven by their recorded evidence and not withheld."""
    return tuple(
        item for item in options(parents) if item.snapshot.revision.revision_id.root not in withheld
    )


def offer_snapshot(base: RevisionRef, offered: tuple[ParentOption, ...]) -> str:
    """Name the immutable offer so a reply cannot cite a parent that was not shown."""
    facts = sorted(item.option_id for item in offered)
    digest = hashlib.sha256("\n".join((base.digest, *facts)).encode())
    return digest.hexdigest()


def _status(record: HypothesisRecord, running: frozenset[str], submitted: frozenset[str]) -> str:
    if record.hypothesis_id in running:
        return "running"
    if record.hypothesis_id in submitted:
        return "submitted"
    return record.strategy.value


def best_part(parts: tuple[CombineSource, ...], space: MetricSpace) -> CombineSource | None:
    """The part with the strongest trusted headline by raw value, in its metric's direction.

    Noise plays no part in choosing it: the composite is then compared with the strongest single
    part. None when no part has a headline, or the headlines are on different metrics or have no
    known direction.
    """
    heads = [(part, head) for part in parts if (head := measurement(part.metric)) is not None]
    if not heads:
        return None
    first = heads[0][1]
    direction = space.direction(first)
    if direction is None or any(
        head.metric != first.metric or space.direction(head) != direction for _, head in heads
    ):
        return None
    sign = 1.0 if direction == "max" else -1.0
    # max keeps the earliest of equal values, so a tie goes to the part applied first.
    return max(heads, key=lambda item: sign * item[1].value)[0]


_VERDICTS = {
    MetricComparison.BETTER: PartsVerdict.BETTER,
    MetricComparison.WORSE: PartsVerdict.WORSE,
    MetricComparison.WITHIN_NOISE: PartsVerdict.WITHIN_NOISE,
}


def versus_parts(
    metrics: tuple[MetricRow, ...], parts: tuple[CombineSource, ...], space: MetricSpace | None
) -> PartsVerdict:
    """How a composite's headline compares with the strongest part's (`best_part`).

    The comparison applies the task's declared noise (a strict one when it declares none).
    Without a headline on either side, a space to compare in, or with the composite on another
    metric than the parts, the verdict is unknown.
    """
    if not metrics or space is None:
        return PartsVerdict.UNKNOWN
    best = best_part(parts, space)
    if best is None:
        return PartsVerdict.UNKNOWN
    found = space.compare(measurement(metrics[0]), measurement(best.metric))
    return _VERDICTS.get(found, PartsVerdict.UNKNOWN)


def _composite(record: HypothesisRecord, space: MetricSpace | None) -> CompositeRow | None:
    """A combine's latest candidate beside its parts; None for a hypothesis no combine started."""
    if not record.combined_from:
        return None
    last = record.rounds[-1] if record.rounds else None
    best = None if space is None else best_part(record.combined_from, space)
    return CompositeRow(
        parts=record.combined_from,
        verdict=versus_parts(() if last is None else last.metrics, record.combined_from, space),
        best_part=None if best is None else best.hypothesis_id,
        relative_noise=0.0 if space is None else space.relative_noise,
        unapplied=() if last is None else last.unapplied,
    )


def _history_row(
    record: HypothesisRecord,
    running: frozenset[str],
    submitted: frozenset[str],
    space: MetricSpace | None = None,
) -> HistoryRow:
    last = record.rounds[-1] if record.rounds else None
    shown = "" if last is None else briefs.tail(last.failure_tail, briefs.FAILURE_CHARS)
    return HistoryRow(
        hypothesis_id=record.hypothesis_id,
        sequence=record.first_sequence,
        title=briefs.bounded(record.title, _HISTORY_TITLE_CHARS),
        status=_status(record, running, submitted),
        strategy=record.strategy.value,
        outcome=None if last is None or last.outcome is None else last.outcome.value,
        summary="" if last is None else briefs.bounded(last.summary, _HISTORY_SUMMARY_CHARS),
        review_passed=None if last is None else last.review_passed,
        accuracy_passed=None if last is None else last.accuracy_passed,
        benchmark_passed=None if last is None else last.benchmark_passed,
        candidate=None if last is None else last.candidate,
        metrics=() if last is None else last.metrics,
        partial=None if last is None else last.partial,
        failure_tail=shown,
        failure_fence=briefs.fence(shown),
        failure=None if last is None else last.failure,
        composite=_composite(record, space),
    )


def _profile_row(
    sequence: int,
    plan_fields: tuple[str, ...],
    work: FinishedWork | None,
    running: RunningWork | None,
) -> ProfileHistoryRow:
    """A profile workstream as the planner sees it: running, or what its capture established."""
    source = work if work is not None else running
    if source is None:
        message = "a profile row has a workstream or a finished record"
        raise ValueError(message)
    plan = source.plan
    refs = () if work is None or work.proof is None else work.proof.refs
    found = None if work is None else reading_of(work.readings, EvidenceKind.PROFILING)
    observed = work is not None and found is not None and found.passed
    status = (
        ProfileHistoryStatus.RUNNING
        if work is None
        else ProfileHistoryStatus.OBSERVED
        if observed
        else ProfileHistoryStatus.FAILED
    )
    diagnosis = "" if not observed or found is None else found.feedback
    printed = "" if observed or work is None else _printed(work, found)
    return ProfileHistoryRow(
        profile_id=plan.work_id,
        sequence=sequence,
        target_hypothesis_id=plan.parent_hypothesis_id,
        revision=source.parent,
        question=briefs.bounded(plan.question, _HISTORY_SUMMARY_CHARS),
        decision_impact=briefs.bounded(plan.decision_impact, _HISTORY_SUMMARY_CHARS),
        status=status,
        evidence_ids=tuple(item.evidence_id for item in refs[:_PROFILE_EVIDENCE_IDS]),
        captured_fields=plan_fields if observed else (),
        metrics=() if not observed or found is None else found.metrics,
        artifacts=(
            ()
            if not observed
            else tuple(artifact.artifact_id.root for item in refs for artifact in item.artifacts)
        ),
        diagnosis=briefs.tail(diagnosis, briefs.FAILURE_CHARS),
        failure=None if work is None or observed else work.failure,
        failure_output=briefs.tail(printed, briefs.FAILURE_CHARS),
        detail_fence=briefs.fence(diagnosis or printed),
    )


def _printed(work: FinishedWork, found: object) -> str:
    """What a failed profile's evidence printed, else what its failure captured."""
    feedback = getattr(found, "feedback", "")
    if feedback:
        return str(feedback)
    return "" if work.failure is None else work.failure.output


def _buildable(item: ParentOption) -> BuildableRow:
    snapshot = item.snapshot
    benchmark = snapshot.benchmark
    return BuildableRow(
        option_id=item.option_id,
        hypothesis_id=snapshot.hypothesis_id,
        revision=snapshot.revision,
        accuracy_evidence=snapshot.accuracy.evidence_id,
        benchmark_evidence=None if benchmark is None else benchmark.evidence_id,
        submission_index=snapshot.submission_index,
        latest_verified=item.latest_verified,
        best_partial=item.best_partial,
        metric=None if benchmark is None else benchmark.headline(),
        partial=None if benchmark is None else benchmark.partial,
    )


@dataclass(frozen=True)
class PlanWorld:
    """Everything one planning event reads of the search."""

    config: DynamicConfig
    base: RevisionRef
    baseline: Baseline | None
    """The input's measurement; None while it is still being measured."""
    hypotheses: tuple[HypothesisRecord, ...]
    running: tuple[RunningWork, ...]
    finished: tuple[FinishedWork, ...]
    refunded: int
    offered: tuple[ParentOption, ...]
    handoffs: tuple[str, ...] = ()
    """The paths of earlier planning calls' handoff files, newest first."""
    offer: ProfileOffer | None = None
    """What the planner may ask of a profile workstream; None when the run does not profile."""
    repairable: frozenset[str] | None = None
    """While the task's plan decides what runs, the failed steps the planner may continue (and
    nothing else); None when the planner may propose any workstream."""
    parked: frozenset[str] = frozenset()
    """The keys of running workstreams parked on their trusted measurement: their agents are
    done and they hold none of the planner's slots (`RunRecord.parked`)."""
    evaluations: EvaluationQueueView | None = None
    input_overdue: InputOverdueView | None = None
    main: RevisionRef | None = None
    """The run's main revision; None means the input."""
    main_measurement: MainMeasurement | None = None
    merges: tuple[MergeRecord, ...] = ()
    """Every finished merge, oldest first."""
    merge_options: MergeOptions = field(default_factory=MergeOptions)
    """Which candidates a merge may name now; its rows are the prompt's mergeable list."""
    space: MetricSpace | None = None
    """The space candidates are compared in: a composite is compared with its parts in it."""

    @property
    def working(self) -> tuple[RunningWork, ...]:
        """Running workstreams whose agents still work."""
        return tuple(item for item in self.running if item.key not in self.parked)

    @property
    def active(self) -> int:
        """Workstreams that have not finished: they may still lead to work to wait for."""
        return len(self.running)

    @property
    def busy(self) -> int:
        """Workstreams whose agents work: each holds one of the planner's slots."""
        return len(self.working)

    @property
    def done(self) -> int:
        """Workstreams that have finished."""
        return len(self.finished)

    @property
    def remaining(self) -> int:
        """Workstreams the run may still schedule."""
        return remaining(self.config, started=self.active + self.done, refunded=self.refunded)

    @property
    def capacity(self) -> int:
        """Free slots the next plan may fill."""
        return free_slots(self.config, active=self.busy, left=self.remaining)

    @property
    def wanted(self) -> int:
        """How many workstreams a reply should schedule: the free slots, or the failed steps."""
        if self.repairable is None:
            return self.capacity
        return min(self.capacity, len(self.repairable))

    @property
    def ledger(self) -> PlanLedger:
        """What plan validation reads: hypotheses, what runs and which IDs are profiles."""
        profiles = {
            item.plan.work_id for item in self.finished if item.plan.kind is WorkKind.PROFILE
        }
        profiles |= {
            item.plan.work_id for item in self.running if item.plan.kind is WorkKind.PROFILE
        }
        merges = {item.plan.work_id for item in self.finished if item.plan.kind is WorkKind.MERGE}
        running_merges = {
            item.plan.work_id for item in self.running if item.plan.kind is WorkKind.MERGE
        }
        return PlanLedger(
            hypotheses=self.hypotheses,
            running=frozenset(item.plan.work_id for item in self.working),
            profile_ids=frozenset(profiles),
            repairable=self.repairable,
            submitted=self._waiting_ids(),
            ranks=self.config.pick == "ranked",
            merge_ids=frozenset(merges | running_merges),
            merging=bool(running_merges),
            merge_options=self.merge_options,
        )

    def _waiting_ids(self) -> frozenset[str]:
        """Work ids with a candidate waiting for its evaluation and no workstream working."""
        working = {item.plan.work_id for item in self.working}
        return frozenset(
            item.plan.work_id
            for item in self.running
            if item.key in self.parked and item.plan.work_id not in working
        )

    def prompt(
        self, problem: PlanProblem | None = None, *, again: InvocationRef | None = None
    ) -> PlannerPrompt | PlannerCorrection:
        """The free-slot planning request: budget, offered parents and compact history.

        ``problem`` says why an earlier reply (or call) was not accepted; ``again`` names the
        turn whose reply this request corrects, when it does.
        """
        running = frozenset(item.plan.work_id for item in self.working)
        submitted = self._waiting_ids()
        rows: list[WorkstreamHistoryRow] = [
            _history_row(item, running, submitted, self.space) for item in self.hypotheses
        ]
        implement_rows = tuple(rows)
        profiles = [
            _profile_row(item.sequence, item.plan.required_fields, item, None)
            for item in self.finished
            if item.plan.kind is WorkKind.PROFILE
        ]
        profiles += [
            _profile_row(item.sequence, item.plan.required_fields, None, item)
            for item in self.running
            if item.plan.kind is WorkKind.PROFILE
        ]
        history = tuple(sorted((*rows, *profiles), key=lambda row: row.sequence))
        shown = history[-_HISTORY_ROWS:]
        shown_sequences = {row.sequence for row in shown}
        seen = None if self.baseline is None else reading(self.baseline)
        prompt = PlannerPrompt(
            capacity=self.capacity,
            in_flight=len(running),
            remaining=self.remaining,
            base_revision=self.base,
            base_accuracy_passed=None if seen is None else seen.accuracy_passed,
            offer_snapshot=offer_snapshot(self.base, self.offered),
            buildable=tuple(_buildable(item) for item in self.offered),
            history=shown,
            older_ids=tuple(
                row.hypothesis_id
                for row in implement_rows
                if isinstance(row, HistoryRow) and row.sequence not in shown_sequences
            ),
            baseline=() if seen is None else seen.metrics,
            input_failure=None if self.baseline is None else failure(self.baseline),
            baseline_pending=self.baseline is None,
            profiling=self.offer is not None,
            profile_fields=() if self.offer is None else tuple(str(f) for f in self.offer.fields),
            handoff_paths=self.handoffs,
            evaluations=self.evaluations,
            input_overdue=self.input_overdue,
            main_revision=self.main or self.base,
            main_measurement=self.main_measurement,
            merge_running=next(
                (item.plan.work_id for item in self.running if item.plan.kind is WorkKind.MERGE),
                None,
            ),
            mergeable=self.merge_options.rows,
            merges=self.merges,
            problem=problem,
        )
        if again is None:
            return prompt
        shared = {
            name: getattr(prompt, name) for name in PlannerPrompt.model_fields if name != "template"
        }
        return PlannerCorrection(of=again, **shared)
