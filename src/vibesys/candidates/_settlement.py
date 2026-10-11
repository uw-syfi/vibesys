"""What a finished attempt asserts to core: assessments, eligibility and retention.

Hypothesis truth and checkpoint utility are separate questions. A measured candidate whose
accuracy passed is retained as a buildable parent even when its benchmark failed; only a
candidate that passed every configured gate, was not rejected by review, has no recorded
failure and beats the input baseline is winner-eligible. Eligibility is decided from evidence
only.
"""

from typing import Literal, Protocol

from vibesys.candidates._baseline import BaselineStage, BaselineState
from vibesys.candidates._config import CandidateConfig
from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading, Measurement, MetricComparison, MetricRow, reading_of
from vs_core.api import (
    AssessmentKind,
    AssessmentProposal,
    EvidenceKind,
    InvocationRef,
    RevisionRef,
    Settle,
)

STOPPED = "stopped by the operator"
_UNSETTLED = frozenset({HypothesisOutcome.BLOCKED, HypothesisOutcome.IMPLEMENTATION_FAILED})


class FinishedAttempt(Protocol):
    """What settlement reads of an attempt, which a strategy's own record provides."""

    @property
    def readings(self) -> tuple[AcceptedReading, ...]:
        """The decoded trusted evaluations of the attempt's candidate."""
        ...

    @property
    def candidate(self) -> RevisionRef | None:
        """The revision the attempt retained, if any."""
        ...

    @property
    def review_passed(self) -> bool | None:
        """The judge's verdict, or None when no review ran."""
        ...

    @property
    def judge_invocation(self) -> InvocationRef | None:
        """The judge turn the verdict came from."""
        ...

    @property
    def outcome(self) -> HypothesisOutcome | None:
        """The implementer's own account of its result."""
        ...

    @property
    def failure(self) -> str | None:
        """The recorded failure, or None."""
        ...


def measurement(row: MetricRow | None) -> Measurement | None:
    """A metric row as the comparable value `MetricSpace` orders."""
    return None if row is None else Measurement(row.name, row.value, row.direction)


def beats_baseline(
    baseline: BaselineState, config: CandidateConfig, reading: AcceptedReading | None
) -> bool:
    """Whether the candidate's headline improves on the baseline beyond noise.

    An input that was not measured leaves nothing to beat, so candidates compete
    on their own readings.
    """
    if baseline.stage is not BaselineStage.MEASURED or not baseline.metrics:
        return True
    comparison = config.metric_space.compare(
        measurement(None if reading is None else reading.headline()),
        measurement(baseline.metrics[0]),
    )
    return comparison is MetricComparison.BETTER


def _gate(reading: AcceptedReading | None, *, configured: bool) -> bool:
    """A configured gate passes only with a decoded, passed reading."""
    return not configured or (reading is not None and reading.passed)


def assessment(
    kind: AssessmentKind, reading: AcceptedReading | None, candidate: RevisionRef | None
) -> tuple[AssessmentProposal, ...]:
    """The assessment one decoded reading asserts about a candidate, or none."""
    if reading is None:
        return ()
    return (
        AssessmentProposal(
            kind=kind,
            verdict="satisfied" if reading.passed else "rejected",
            sources=(reading.key,),
            candidate=candidate,
            schema_version=1,
        ),
    )


def _review(record: FinishedAttempt) -> tuple[AssessmentProposal, ...]:
    if record.judge_invocation is None or record.review_passed is None:
        return ()
    return (
        AssessmentProposal(
            kind=AssessmentKind.LOCAL_VALIDATION,
            verdict="satisfied" if record.review_passed else "rejected",
            sources=(record.judge_invocation,),
            candidate=record.candidate,
            schema_version=1,
        ),
    )


def outcome_of(record: FinishedAttempt) -> Literal["succeeded", "failed", "cancelled", "blocked"]:
    """The settlement outcome the record's scientific result maps to."""
    if record.failure == STOPPED:
        return "cancelled"
    if record.outcome is HypothesisOutcome.BLOCKED:
        return "blocked"
    return "succeeded" if record.failure is None else "failed"


def settle_for(record: FinishedAttempt, baseline: BaselineState, config: CandidateConfig) -> Settle:
    """The assessments, eligibility and retention to propose for a finished attempt."""
    accuracy = reading_of(record.readings, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(record.readings, EvidenceKind.BENCHMARK)
    measured = bool(record.readings) and record.candidate is not None
    buildable = measured and _gate(accuracy, configured=config.accuracy_configured)
    eligible = (
        buildable
        and _gate(benchmark, configured=config.benchmark_configured)
        and record.review_passed is not False
        and record.outcome not in _UNSETTLED
        and record.failure is None
        and beats_baseline(baseline, config, benchmark)
    )
    return Settle(
        assessments=(
            *assessment(AssessmentKind.CORRECTNESS, accuracy, record.candidate),
            *assessment(AssessmentKind.BENCHMARK, benchmark, record.candidate),
            *_review(record),
        ),
        eligible=eligible,
        retention="candidate" if buildable else "discard",
        outcome=outcome_of(record),
        candidate=record.candidate if buildable else None,
    )
