"""The run's main revision: where new work starts, and what merges move it.

Main starts at the run's input. Only a merge workstream moves it: when the merger's commit
passed the trusted accuracy check and measures no worse than main, main becomes that commit; a
refusal, a failed check, a regression or a lost merge leaves it where it was. Each move bumps main's generation, which tells the search that a
hypothesis started on an older main should rebase. `MainLine` is folded by the run record from
finished merge workstreams and read by planning; like the record, it is rebuilt by replay.

What a merge must beat is a closed `MainReading`: pending while the input's measurement has not
landed (nothing is mergeable and main cannot move), no headline when main and the input have no
headline metric (any verified candidate beats it), or main's headline metric.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from vibesys.candidates import api as candidates
from vibesys.metrics import MetricRow, MetricSpace, reading_of
from vibesys.orchestration.dynamic.search._outcomes import WorkstreamDone
from vibesys.orchestration.dynamic.search._parents import ParentOption
from vibesys.orchestration.dynamic.search._prompts import MergeableRow
from vibesys.orchestration.dynamic.search._values import (
    Failure,
    FailureKind,
    MainMeasurement,
    MergeOutcome,
    MergeRecord,
    ViolationCode,
)
from vs_core.api.search import EvidenceKind, RevisionRef, TaskTerms, Value


@dataclass(frozen=True)
class BaselinePending:
    """The input's measurement has not landed: main has no reading yet to compare a merge with."""


@dataclass(frozen=True)
class NoHeadline:
    """Main and the input have no headline metric (an unmeasurable input, say): nothing to beat."""


@dataclass(frozen=True)
class Headline:
    """Main's headline metric now: its own measurement's, else the input's."""

    row: MetricRow


type MainReading = BaselinePending | NoHeadline | Headline
"""What a merge candidate and a merged commit are compared with."""


def checkable(terms: TaskTerms) -> bool:
    """Whether the task has a trusted accuracy check, without which main can never move."""
    return terms.accuracy_stage is not None and terms.accuracy_stage in terms.measured


@dataclass(frozen=True)
class MergeOptions:
    """Which candidates a merge may name now, as `MainLine.options` decides for prompt and checks.

    ``rows`` are the mergeable candidates the planner is shown; ``barred`` names, for each other
    latest verified candidate, the rule it breaks. A hypothesis in neither is not buildable.
    """

    checkable: bool = False
    """Whether the task can check a merge at all (see `checkable`)."""
    rows: tuple[MergeableRow, ...] = ()
    barred: tuple[tuple[str, ViolationCode], ...] = ()

    def bar(self, hypothesis_id: str) -> ViolationCode | None:
        """The rule ``hypothesis_id``'s latest verified candidate breaks as a merge candidate."""
        return dict(self.barred).get(hypothesis_id)

    def mergeable(self, hypothesis_id: str) -> bool:
        """Whether a merge may name ``hypothesis_id`` now."""
        return any(row.hypothesis_id == hypothesis_id for row in self.rows)


class MainState(Value):
    """Everything `MainLine` knows but the input's measurement, to resume main from.

    A restart after a code change resumes main where it was (`MainLine.resumed`); the record
    keeps the input's measurement.
    """

    revision: RevisionRef
    generation: int
    measurement: MainMeasurement | None
    merges: tuple[MergeRecord, ...]
    rounds: tuple[candidates.EligibleRound, ...]
    merged: tuple[tuple[str, str], ...]


@dataclass
class MainLine:
    """The run's main revision, its latest trusted measurement and every finished merge."""

    revision: RevisionRef
    """Main now: the input until a merge moves it."""
    baseline: candidates.Baseline | None
    """The input's own measurement, which main is compared with until a merge measures it; None
    while it has not landed."""
    generation: int = 0
    """How many times main has moved."""
    measurement: MainMeasurement | None = None
    """The trusted measurement of main now; None while main is the input."""
    merges: list[MergeRecord] = field(default_factory=list)
    rounds: list[candidates.EligibleRound] = field(default_factory=list)
    """The merged mains that are winner-eligible, as the winner is chosen among them."""
    merged: set[tuple[str, str]] = field(default_factory=set)
    """The (hypothesis, revision) candidates a merge already brought into main."""

    def state(self) -> MainState:
        """What main knows, to resume it from (`resumed`); ``merged`` sorted, as a set has no order."""
        return MainState(
            revision=self.revision,
            generation=self.generation,
            measurement=self.measurement,
            merges=tuple(self.merges),
            rounds=tuple(self.rounds),
            merged=tuple(sorted(self.merged)),
        )

    @classmethod
    def resumed(cls, baseline: candidates.Baseline | None, state: MainState) -> "MainLine":
        """The main ``state`` describes, compared with the input's ``baseline``."""
        return cls(
            revision=state.revision,
            baseline=baseline,
            generation=state.generation,
            measurement=state.measurement,
            merges=list(state.merges),
            rounds=list(state.rounds),
            merged=set(state.merged),
        )

    def reading(self) -> MainReading:
        """What main measures now: its own measurement, else the input's, else pending."""
        if self.measurement is not None:
            metrics = self.measurement.metrics
        elif self.baseline is None:
            return BaselinePending()
        else:
            seen = candidates.reading(self.baseline)
            metrics = () if seen is None else seen.metrics
        return Headline(metrics[0]) if metrics else NoHeadline()

    def headline(self) -> MetricRow | None:
        """Main's headline metric now, when it has one."""
        reading = self.reading()
        return reading.row if isinstance(reading, Headline) else None

    def _benchmark_passed(self) -> bool | None:
        if self.measurement is not None:
            return self.measurement.benchmark_passed
        seen = None if self.baseline is None else candidates.reading(self.baseline)
        return None if seen is None else seen.benchmark_passed

    def options(
        self, offered: tuple[ParentOption, ...], terms: TaskTerms, merge_ids: frozenset[str]
    ) -> MergeOptions:
        """Which candidates a merge may name now: the one rule the prompt and validation share.

        Only the latest verified candidate of a hypothesis is judged; an older revision is not a
        merge candidate at all. Every offered candidate already passed the trusted accuracy check. On a task without a trusted accuracy check nothing is mergeable,
        since main could never move; while the input's measurement is pending nothing is either,
        since there is nothing yet to compare a candidate with.
        """
        if not checkable(terms):
            return MergeOptions(checkable=False)
        space = candidates.space_of(terms)
        main = self.reading()
        rows: list[MergeableRow] = []
        barred: list[tuple[str, ViolationCode]] = []
        for item in offered:
            if not item.latest_verified:
                continue
            snapshot = item.snapshot
            benchmark = snapshot.benchmark
            headline = None if benchmark is None else benchmark.headline()
            key = (snapshot.hypothesis_id, snapshot.revision.revision_id.root)
            bar: ViolationCode | None = None
            if snapshot.hypothesis_id in merge_ids:
                bar = ViolationCode.MERGE_CANDIDATE_IS_MAIN
            elif key in self.merged:
                bar = ViolationCode.MERGE_CANDIDATE_MERGED
            elif not _beats(headline, main, space):
                bar = (
                    ViolationCode.MERGE_BASELINE_PENDING
                    if isinstance(main, BaselinePending)
                    else ViolationCode.MERGE_CANDIDATE_NOT_BETTER
                )
            if bar is not None:
                barred.append((snapshot.hypothesis_id, bar))
                continue
            rows.append(
                MergeableRow(
                    hypothesis_id=snapshot.hypothesis_id,
                    revision=snapshot.revision,
                    metric=headline,
                )
            )
        return MergeOptions(checkable=True, rows=tuple(rows), barred=tuple(barred))

    def start_of(self, start: RevisionRef, lineage: Mapping[str, frozenset[str]]) -> RevisionRef:
        """Where a tree's own change against main starts, given where it started (``start``).

        ``lineage`` maps each revision the tree holds to the revisions that one holds. A merge
        that brought in a candidate the tree holds, whose own change started at ``start``, put
        that candidate wholly into main, so the tree's own change starts there instead: from it,
        the tree holds only its own change, and main holds the candidate. Of several such
        candidates, the one holding all the others. The merge commit is never a start: the
        merger may edit beyond its sources (a fix-up), and a diff from it would remove the edit.
        """
        for merge in self.merges:
            if merge.outcome is not MergeOutcome.MERGED or merge.revision is None:
                continue
            took = [
                item.revision
                for item in merge.candidates
                if item.revision.revision_id.root in lineage
                and item.built_on is not None
                and item.built_on.revision_id.root == start.revision_id.root
            ]
            latest = [
                found
                for found in took
                if all(item.revision_id.root in lineage[found.revision_id.root] for item in took)
            ]
            if latest:
                start = latest[0]
        return start

    def finished(self, done: WorkstreamDone, terms: TaskTerms) -> MergeRecord:
        """Fold a finished merge workstream: move main when its commit is sound, record it.

        Main moves only to a retained commit that passed the trusted accuracy check, whose
        headline metric is not worse than main's and that fails no benchmark check main passed.
        A commit that passed accuracy but measures worse is recorded as regressed and main stays.
        While the input's measurement is pending there is nothing to compare the commit with, so
        it is unsound and main stays (a merge's measurement waits for the input, so this is a
        guard, not a path a run takes).
        """
        start = done.start
        report = done.merge
        accuracy = reading_of(done.evidence.readings, EvidenceKind.CORRECTNESS)
        benchmark = reading_of(done.evidence.readings, EvidenceKind.BENCHMARK)
        settlement = done.settlement
        accuracy_passed = None if accuracy is None else accuracy.passed
        benchmark_passed = None if benchmark is None else benchmark.passed
        metrics = () if benchmark is None else benchmark.metrics
        reading = self.reading()
        main = reading.row if isinstance(reading, Headline) else None
        committed = (
            not isinstance(reading, BaselinePending)
            and report is not None
            and report.outcome is MergeOutcome.MERGED
            and done.failure is None
            and settlement is not None
            and settlement.retention == "candidate"
            and settlement.candidate is not None
            and accuracy_passed is True
        )
        regressed = committed and self._regresses(
            main, metrics, benchmark_passed=benchmark_passed, terms=terms
        )
        sound = committed and not regressed
        failure = done.failure
        if report is not None and report.outcome is MergeOutcome.REFUSED:
            outcome = MergeOutcome.REFUSED
        elif sound:
            outcome, failure = MergeOutcome.MERGED, None
        else:
            outcome = MergeOutcome.FAILED
            if regressed:
                failure = Failure(kind=FailureKind.MERGE_REGRESSED)
            elif failure is None:
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
            accuracy_passed=accuracy_passed,
            benchmark_passed=benchmark_passed,
            metrics=metrics,
            main_metrics=() if main is None or not committed else (main,),
            main_benchmark_passed=self._benchmark_passed() if committed else None,
            failure=failure,
        )
        self.merges.append(record)
        if sound and settlement is not None and settlement.candidate is not None:
            self._advance(record, settlement.candidate)
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

    def _regresses(
        self,
        main: MetricRow | None,
        metrics: tuple[MetricRow, ...],
        *,
        benchmark_passed: bool | None,
        terms: TaskTerms,
    ) -> bool:
        """Whether a commit measures worse than main beyond noise, or fails a check main passed."""
        new = metrics[0] if metrics else None
        worse = main is not None and candidates.improves_on(
            candidates.measurement(main),
            candidates.measurement(new),
            space=candidates.space_of(terms),
            when_input_unmeasured=False,
        )
        lost_check = benchmark_passed is not True and self._benchmark_passed() is True
        return worse or lost_check

    def _advance(self, record: MergeRecord, revision: RevisionRef) -> None:
        """Move main to a sound merged commit."""
        self.measurement = MainMeasurement(
            revision=revision,
            merge_id=record.merge_id,
            accuracy_passed=record.accuracy_passed,
            benchmark_passed=record.benchmark_passed,
            metrics=record.metrics,
            previous=record.main_metrics,
        )
        self.revision = revision
        self.generation += 1
        self.merged |= {
            (source.hypothesis_id, source.revision.revision_id.root) for source in record.candidates
        }


def _beats(headline: MetricRow | None, main: MainReading, space: MetricSpace) -> bool:
    """Whether a candidate's headline metric beats main beyond noise; nothing beats a pending one."""
    match main:
        case BaselinePending():
            return False
        case NoHeadline():
            return True
        case Headline(row=row):
            return candidates.improves_on(
                candidates.measurement(headline),
                candidates.measurement(row),
                space=space,
                when_input_unmeasured=False,
            )
