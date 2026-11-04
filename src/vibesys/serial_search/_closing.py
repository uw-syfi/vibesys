"""What a finished round asserts and records: pure judgments over its readings.

A round ends by asserting to core what it established (`settle`) and by closing in the ledger
with the benchmark it saw (`benchmark_outcome`). The shared candidate policy decides assessments
and eligibility; the two cases a serial search adds are here. A candidate no trusted reading made
buildable is kept without being adoptable, and the input's own reading sets what a candidate must
beat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.candidates.api import (
    Baseline,
    gates_of,
    improves_on,
    input_metrics,
    measurement,
    settle_for,
    space_of,
)
from vibesys.candidates.api import turned_down as checks_turned_down
from vibesys.hypothesis import CandidateDisposition
from vibesys.metrics import FrameworkBenchmarkOutcome, MetricRow, reading_of
from vs_core.api.search import Assessment, EvidenceKind

if TYPE_CHECKING:
    from vibesys.hypothesis import HypothesisOutcome
    from vibesys.metrics import AcceptedReading
    from vs_core.api.search import InvocationRef, RevisionRef, TaskTerms


@dataclass(frozen=True)
class FinishedRound:
    """The round as the candidate policy reads a finished attempt."""

    readings: tuple[AcceptedReading, ...]
    candidate: RevisionRef | None
    review_passed: bool | None
    failure: str | None
    passed: bool
    judge_invocation: InvocationRef | None = None
    outcome: HypothesisOutcome | None = None
    disposition: CandidateDisposition = CandidateDisposition.UNASSESSED
    """What the implementer recommends doing with the checkpoint it leaves."""


def turned_down(finished: FinishedRound, *, unsettled: frozenset[HypothesisOutcome]) -> bool:
    """Whether the round's own checks turned its work down, whatever its workspace keeps.

    The shared rule (`candidates.turned_down`: a failed review, an unsettled outcome or a
    rejecting trusted gate), and the implementer's own recommendation to discard the checkpoint.
    The workspace may still keep the code as work in progress, which the next round builds on
    unless its designer reverts; the round did not succeed, so its workstream is rejected.
    """
    discarded = finished.disposition is CandidateDisposition.DISCARD
    return discarded or checks_turned_down(finished, unsettled=unsettled)


def beats_input(
    baseline: Baseline, readings: tuple[AcceptedReading, ...], terms: TaskTerms
) -> bool:
    """Whether the candidate's benchmark beats the input's beyond noise.

    The bar is `input_metrics` (a measured input's benchmark, or that of one that failed only
    its accuracy check); an input with no bar leaves nothing to beat.
    """
    benchmark = reading_of(readings, EvidenceKind.BENCHMARK)
    bar = input_metrics(baseline)
    return improves_on(
        measurement(None if benchmark is None else benchmark.headline()),
        measurement(bar[0] if bar else None),
        space=space_of(terms),
        when_input_unmeasured=True,
    )


def settle(
    finished: FinishedRound,
    *,
    baseline: Baseline,
    terms: TaskTerms,
    unsettled: frozenset[HypothesisOutcome],
) -> tuple[RevisionRef | None, Assessment]:
    """The revision to keep and what to assert about it when the round's workspace closes.

    A round that passed review but was not measured keeps its checkpoint as a buildable parent
    that can never be adopted; one that did not pass keeps it as work in progress. A candidate
    nothing made buildable is discarded, and then no revision is kept.
    """
    settled = settle_for(
        finished,
        gates=gates_of(terms),
        beats_input=beats_input(baseline, finished.readings, terms),
        unsettled=unsettled,
    )
    if (
        settled.retention == "discard"
        and finished.candidate is not None
        and finished.failure is None
    ):
        settled = settled.model_copy(
            update={
                "retention": "candidate" if finished.passed else "wip",
                "candidate": finished.candidate,
                "eligible": False,
            }
        )
    return settled.candidate, Assessment(
        outcome=settled.outcome,
        assessments=settled.assessments,
        eligible=settled.eligible,
        retention=settled.retention,
    )


def benchmark_outcome(
    readings: tuple[AcceptedReading, ...],
) -> tuple[FrameworkBenchmarkOutcome, float | None]:
    """The benchmark a round saw, as the ledger records it, and its headline value."""
    reading = reading_of(readings, EvidenceKind.BENCHMARK)
    headline: MetricRow | None = None if reading is None else reading.headline()
    if reading is None or headline is None:
        return FrameworkBenchmarkOutcome(
            feedback=None if reading is None else reading.feedback
        ), None
    return (
        FrameworkBenchmarkOutcome(
            feedback=reading.feedback,
            metric_name=headline.name,
            metric_value=headline.value,
            metric_direction=headline.direction,
            metric_unit=headline.unit,
            row={row.name: row.value for row in reading.metrics},
        ),
        headline.value,
    )


def failed_gate(readings: tuple[AcceptedReading, ...]) -> str | None:
    """The feedback of the first trusted gate (accuracy, then benchmark) that rejected, else None."""
    for kind in (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK):
        reading = reading_of(readings, kind)
        if reading is not None and not reading.passed:
            return reading.feedback or f"{reading.kind.value} gate failed"
    return None


def accuracy_passed(readings: tuple[AcceptedReading, ...]) -> bool:
    """Whether the accuracy gate was read and passed."""
    reading = reading_of(readings, EvidenceKind.CORRECTNESS)
    return reading is not None and reading.passed
