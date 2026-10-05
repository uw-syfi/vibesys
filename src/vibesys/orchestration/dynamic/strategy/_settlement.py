"""What a finished workstream asserts to core: assessments, eligibility and retention.

Hypothesis truth and checkpoint utility are separate questions. A measured
candidate whose accuracy passed is retained as a buildable parent even when its
benchmark failed; only a candidate that passed every configured gate, was not
rejected by review and beats the input baseline is winner-eligible.
"""

from typing import Literal

from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import Measurement, MetricComparison
from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._rows import AcceptedReading, MetricRow, reading_of
from vibesys.orchestration.dynamic.strategy._state import (
    AttemptRecord,
    BaselineStage,
    DynamicStrategyState,
    WorkKind,
)
from vs_core.api import AssessmentKind, AssessmentProposal, EvidenceKind, Settle

STOPPED = "stopped by the operator"
_UNSETTLED = frozenset({HypothesisOutcome.BLOCKED, HypothesisOutcome.IMPLEMENTATION_FAILED})


def measurement(row: MetricRow | None) -> Measurement | None:
    """A metric row as the comparable value `MetricSpace` orders."""
    return None if row is None else Measurement(row.name, row.value, row.direction)


def beats_baseline(
    state: DynamicStrategyState, config: DynamicConfig, reading: AcceptedReading | None
) -> bool:
    """Whether the candidate's headline improves on the baseline beyond noise.

    An input that was not measured leaves nothing to beat, so candidates compete
    on their own readings.
    """
    baseline = state.baseline
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


def _assessment(
    kind: AssessmentKind, reading: AcceptedReading | None, record: AttemptRecord
) -> tuple[AssessmentProposal, ...]:
    if reading is None:
        return ()
    return (
        AssessmentProposal(
            kind=kind,
            verdict="satisfied" if reading.passed else "rejected",
            sources=(reading.key,),
            candidate=record.candidate,
            schema_version=1,
        ),
    )


def _review(record: AttemptRecord) -> tuple[AssessmentProposal, ...]:
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


def _profile_settle(record: AttemptRecord) -> Settle:
    profile = reading_of(record.readings, EvidenceKind.PROFILING)
    return Settle(
        assessments=_assessment(AssessmentKind.PROFILING, profile, record),
        eligible=False,
        retention="discard",
        outcome="succeeded" if record.failure is None else "failed",
        candidate=None,
    )


def outcome_of(record: AttemptRecord) -> Literal["succeeded", "failed", "cancelled", "blocked"]:
    """The settlement outcome the record's scientific result maps to."""
    if record.failure == STOPPED:
        return "cancelled"
    if record.outcome is HypothesisOutcome.BLOCKED:
        return "blocked"
    return "succeeded" if record.failure is None else "failed"


def settle_for(record: AttemptRecord, state: DynamicStrategyState, config: DynamicConfig) -> Settle:
    """The assessments, eligibility and retention to propose for a finished attempt."""
    if record.plan.kind is WorkKind.PROFILE:
        return _profile_settle(record)
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
        and beats_baseline(state, config, benchmark)
    )
    return Settle(
        assessments=(
            *_assessment(AssessmentKind.CORRECTNESS, accuracy, record),
            *_assessment(AssessmentKind.BENCHMARK, benchmark, record),
            *_review(record),
        ),
        eligible=eligible,
        retention="candidate" if buildable else "discard",
        outcome=outcome_of(record),
        candidate=record.candidate if buildable else None,
    )
