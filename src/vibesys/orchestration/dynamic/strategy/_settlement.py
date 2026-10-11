"""What a finished workstream asserts to core: dynamic's profile case over the shared policy.

Candidate settlement (assessments, eligibility, retention) is `vibesys.candidates`. A profile
workstream measures a revision without proposing one, so it settles as ineligible and discarded.
"""

from vibesys.candidates import api as candidates
from vibesys.metrics import reading_of
from vibesys.orchestration.dynamic.strategy._config import DynamicConfig
from vibesys.orchestration.dynamic.strategy._state import (
    AttemptRecord,
    DynamicStrategyState,
    WorkKind,
)
from vs_core.api import AssessmentKind, EvidenceKind, Settle


def _profile_settle(record: AttemptRecord) -> Settle:
    profile = reading_of(record.readings, EvidenceKind.PROFILING)
    return Settle(
        assessments=candidates.assessment(AssessmentKind.PROFILING, profile, record.candidate),
        eligible=False,
        retention="discard",
        outcome="succeeded" if record.failure is None else "failed",
        candidate=None,
    )


def settle_for(record: AttemptRecord, state: DynamicStrategyState, config: DynamicConfig) -> Settle:
    """The assessments, eligibility and retention to propose for a finished attempt."""
    if record.plan.kind is WorkKind.PROFILE:
        return _profile_settle(record)
    return candidates.settle_for(record, state.baseline, config)
