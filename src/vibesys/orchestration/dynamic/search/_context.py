"""What the planning call reads of the search: budgets, offered parents and the compact history.

`PlanWorld` is a read-only slice of the search's record, built for one planning ask. Everything
on it is a pure function of that slice; the prompt it builds is a typed value, never text.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from vibesys.candidates.api import Baseline, failure, reading
from vibesys.metrics import reading_of
from vibesys.orchestration.dynamic.models import ProfileOffer
from vibesys.orchestration.dynamic.search import _briefs as briefs
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._parents import ParentOption, ParentSnapshot, options
from vibesys.orchestration.dynamic.search._plan import PlanLedger
from vibesys.orchestration.dynamic.search._prompts import (
    BuildableRow,
    HistoryRow,
    PlannerCorrection,
    PlannerPrompt,
    ProfileHistoryRow,
    ProfileHistoryStatus,
    WorkstreamHistoryRow,
)
from vibesys.orchestration.dynamic.search._values import (
    FinishedWork,
    HypothesisRecord,
    PlanProblem,
    WorkKind,
    WorkPlan,
)
from vs_core.api.search import EvidenceKind, InvocationRef, RevisionRef


class RunningWork(Protocol):
    """What the planner is told of a workstream that has not finished."""

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


def _status(record: HypothesisRecord, running: frozenset[str]) -> str:
    return "running" if record.hypothesis_id in running else record.strategy.value


def _history_row(record: HypothesisRecord, running: frozenset[str]) -> HistoryRow:
    last = record.rounds[-1] if record.rounds else None
    shown = "" if last is None else briefs.tail(last.failure_tail, briefs.FAILURE_CHARS)
    return HistoryRow(
        hypothesis_id=record.hypothesis_id,
        sequence=record.first_sequence,
        title=briefs.bounded(record.title, _HISTORY_TITLE_CHARS),
        status=_status(record, running),
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

    @property
    def active(self) -> int:
        """Workstreams that have not finished."""
        return len(self.running)

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
        return free_slots(self.config, active=self.active, left=self.remaining)

    @property
    def ledger(self) -> PlanLedger:
        """What plan validation reads: hypotheses, what runs and which IDs are profiles."""
        profiles = {
            item.plan.work_id for item in self.finished if item.plan.kind is WorkKind.PROFILE
        }
        profiles |= {
            item.plan.work_id for item in self.running if item.plan.kind is WorkKind.PROFILE
        }
        return PlanLedger(
            hypotheses=self.hypotheses,
            running=frozenset(item.plan.work_id for item in self.running),
            profile_ids=frozenset(profiles),
        )

    def prompt(
        self, problem: PlanProblem | None = None, *, again: InvocationRef | None = None
    ) -> PlannerPrompt | PlannerCorrection:
        """The free-slot planning request: budget, offered parents and compact history.

        ``problem`` says why an earlier reply (or call) was not accepted; ``again`` names the
        turn whose reply this request corrects, when it does.
        """
        running = frozenset(item.plan.work_id for item in self.running)
        rows: list[WorkstreamHistoryRow] = [_history_row(item, running) for item in self.hypotheses]
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
            problem=problem,
        )
        if again is None:
            return prompt
        shared = {
            name: getattr(prompt, name) for name in PlannerPrompt.model_fields if name != "template"
        }
        return PlannerCorrection(of=again, **shared)
