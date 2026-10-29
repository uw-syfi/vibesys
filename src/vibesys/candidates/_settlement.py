"""What a finished attempt asserts to core: assessments, eligibility and retention.

Hypothesis truth and checkpoint utility are separate questions. A measured candidate whose
accuracy passed is retained as a buildable parent even when its benchmark failed; only a
candidate that passed every configured gate, was not rejected by review, has no recorded
failure and beats the input baseline is winner-eligible. Eligibility is decided from evidence
only.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading, Measurement, MetricRow, reading_of
from vs_core.api import (
    AssessmentKind,
    AssessmentProposal,
    EvidenceKind,
    InvocationRef,
    RevisionRef,
    Settle,
)

STOPPED = "stopped by the operator"


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


_GATES = (EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK)


def turned_down(record: FinishedAttempt, *, unsettled: frozenset[HypothesisOutcome]) -> bool:
    """Whether the attempt's own checks turned its work down, whatever its workspace keeps.

    Its review rejected it, its implementer reported an outcome in ``unsettled`` (it could not
    implement the idea, or was blocked), or a trusted gate rejected its candidate. A strategy may
    still keep the code as work in progress a later attempt builds on (retention is about reuse);
    the attempt itself did not succeed, so its workstream is rejected. A recorded failure is not
    the attempt's own verdict (the machinery may have failed it), so it is not one of these.
    """
    return (
        record.review_passed is False
        or record.outcome in unsettled
        or any(
            reading is not None and not reading.passed
            for reading in (reading_of(record.readings, kind) for kind in _GATES)
        )
    )


def outcome_of(record: FinishedAttempt) -> Literal["succeeded", "failed", "cancelled", "blocked"]:
    """The settlement outcome the record's scientific result maps to."""
    if record.failure == STOPPED:
        return "cancelled"
    if record.outcome is HypothesisOutcome.BLOCKED:
        return "blocked"
    return "succeeded" if record.failure is None else "failed"


@dataclass(frozen=True)
class Gates:
    """Which gates a candidate must pass to be winner-eligible; the task's evaluation says."""

    accuracy: bool
    benchmark: bool


def settle_for(
    record: FinishedAttempt,
    *,
    gates: Gates,
    beats_input: bool,
    unsettled: frozenset[HypothesisOutcome],
) -> Settle:
    """The assessments, eligibility and retention to propose for a finished attempt.

    ``beats_input`` is the search's own verdict (see `improves_on`); ``unsettled`` are the
    implementer outcomes that make a candidate ineligible.
    """
    accuracy = reading_of(record.readings, EvidenceKind.CORRECTNESS)
    benchmark = reading_of(record.readings, EvidenceKind.BENCHMARK)
    measured = bool(record.readings) and record.candidate is not None
    buildable = measured and _gate(accuracy, configured=gates.accuracy)
    eligible = (
        buildable
        and _gate(benchmark, configured=gates.benchmark)
        and record.review_passed is not False
        and record.outcome not in unsettled
        and record.failure is None
        and beats_input
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
