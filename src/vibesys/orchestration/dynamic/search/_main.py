"""The run's main revision: where new work starts, and what merges move it.

Main starts at the run's input. Only a merge workstream moves it: when the merger's commit
passed the trusted accuracy check, main becomes that commit; a refusal, a failed check or a lost
merge leaves it where it was. Each move bumps main's generation, which tells the search that a
hypothesis started on an older main should rebase. `MainLine` is folded by the run record from
finished merge workstreams and read by planning; like the record, it is rebuilt by replay.
"""

from dataclasses import dataclass, field

from vibesys.candidates import api as candidates
from vibesys.metrics import MetricRow, reading_of
from vibesys.orchestration.dynamic.search._outcomes import WorkstreamDone
from vibesys.orchestration.dynamic.search._parents import ParentOption
from vibesys.orchestration.dynamic.search._prompts import MergeableRow
from vibesys.orchestration.dynamic.search._values import (
    Failure,
    FailureKind,
    MainMeasurement,
    MergeOutcome,
    MergeRecord,
)
from vs_core.api.search import EvidenceKind, RevisionRef, TaskTerms


@dataclass
class MainLine:
    """The run's main revision, its latest trusted measurement and every finished merge."""

    revision: RevisionRef
    """Main now: the input until a merge moves it."""
    baseline: candidates.Baseline
    """The input's own measurement, which main is compared with until a merge measures it."""
    generation: int = 0
    """How many times main has moved."""
    measurement: MainMeasurement | None = None
    """The trusted measurement of main now; None while main is the input."""
    merges: list[MergeRecord] = field(default_factory=list)
    rounds: list[candidates.EligibleRound] = field(default_factory=list)
    """The merged mains that are winner-eligible, as the winner is chosen among them."""
    merged: set[tuple[str, str]] = field(default_factory=set)
    """The (hypothesis, revision) candidates a merge already brought into main."""

    def headline(self) -> MetricRow | None:
        """Main's headline metric now: its own measurement, else the input's."""
        if self.measurement is not None:
            return self.measurement.metrics[0] if self.measurement.metrics else None
        seen = candidates.reading(self.baseline)
        return seen.metrics[0] if seen is not None and seen.metrics else None

    def _benchmark_passed(self) -> bool | None:
        if self.measurement is not None:
            return self.measurement.benchmark_passed
        seen = candidates.reading(self.baseline)
        return None if seen is None else seen.benchmark_passed

    def mergeable(
        self, offered: tuple[ParentOption, ...], terms: TaskTerms, merge_ids: frozenset[str]
    ) -> tuple[MergeableRow, ...]:
        """The latest buildable candidates that pass accuracy and beat main's latest measurement.

        A candidate a merge already brought into main, and a merged main itself, are not listed.
        """
        space = candidates.space_of(terms)
        main = candidates.measurement(self.headline())
        rows = []
        for item in offered:
            snapshot = item.snapshot
            benchmark = snapshot.benchmark
            headline = None if benchmark is None else benchmark.headline()
            key = (snapshot.hypothesis_id, snapshot.revision.revision_id.root)
            if (
                not item.latest_verified
                or snapshot.hypothesis_id in merge_ids
                or key in self.merged
                or not snapshot.accuracy.passed
                or not candidates.improves_on(
                    candidates.measurement(headline), main, space=space, when_input_unmeasured=True
                )
            ):
                continue
            rows.append(
                MergeableRow(
                    hypothesis_id=snapshot.hypothesis_id,
                    revision=snapshot.revision,
                    metric=headline,
                )
            )
        return tuple(rows)

    def finished(self, done: WorkstreamDone, terms: TaskTerms) -> MergeRecord:
        """Fold a finished merge workstream: move main when its commit is sound, record it."""
        start = done.start
        report = done.merge
        accuracy = reading_of(done.evidence.readings, EvidenceKind.CORRECTNESS)
        benchmark = reading_of(done.evidence.readings, EvidenceKind.BENCHMARK)
        settlement = done.settlement
        sound = (
            report is not None
            and report.outcome is MergeOutcome.MERGED
            and done.failure is None
            and settlement is not None
            and settlement.retention == "candidate"
            and settlement.candidate is not None
        )
        outcome = MergeOutcome.MERGED if sound else MergeOutcome.FAILED
        failure = done.failure
        if report is not None and report.outcome is MergeOutcome.REFUSED:
            outcome = MergeOutcome.REFUSED
        elif not sound and failure is None:
            failure = Failure(kind=FailureKind.MERGE_UNSOUND)
        record = MergeRecord(
            merge_id=start.plan.work_id,
            sequence=start.sequence,
            candidates=start.merge,
            base=start.parent,
            outcome=outcome,
            summary="" if report is None else report.summary,
            reason="" if report is None else report.reason,
            revision=None if report is None else report.revision,
            accuracy_passed=None if accuracy is None else accuracy.passed,
            benchmark_passed=None if benchmark is None else benchmark.passed,
            metrics=() if benchmark is None else benchmark.metrics,
            failure=None if outcome is MergeOutcome.MERGED else failure,
        )
        self.merges.append(record)
        if sound and settlement is not None and settlement.candidate is not None:
            self._advance(record, settlement.candidate, terms)
            if settlement.eligible:
                self.rounds.append(
                    candidates.EligibleRound(
                        hypothesis_id=record.merge_id,
                        sequence=record.sequence,
                        settlement=settlement.settlement_id,
                        candidate=settlement.candidate,
                        metrics=record.metrics,
                    )
                )
        return record

    def _advance(self, record: MergeRecord, revision: RevisionRef, terms: TaskTerms) -> None:
        """Move main to a sound merged commit, and say whether it measures worse than before."""
        previous = self.headline()
        new = record.metrics[0] if record.metrics else None
        worse = previous is not None and candidates.improves_on(
            candidates.measurement(previous),
            candidates.measurement(new),
            space=candidates.space_of(terms),
            when_input_unmeasured=False,
        )
        lost_check = record.benchmark_passed is False and self._benchmark_passed() is True
        self.measurement = MainMeasurement(
            revision=revision,
            merge_id=record.merge_id,
            accuracy_passed=record.accuracy_passed,
            benchmark_passed=record.benchmark_passed,
            metrics=record.metrics,
            previous=() if previous is None else (previous,),
            regressed=worse or lost_check,
        )
        self.revision = revision
        self.generation += 1
        self.merged |= {
            (source.hypothesis_id, source.revision.revision_id.root) for source in record.candidates
        }
