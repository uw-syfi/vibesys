"""What a finished round asserts to core: single's cadence cases over the shared policy.

Candidate settlement (assessments, eligibility, retention) is `vibesys.candidates`. Single adds
two cases of its own. A round the cadence did not measure passed its self-review but has no
trusted reading: it retains its checkpoint as a buildable parent that can never be adopted
(`candidate`, ineligible). A round that failed after producing a checkpoint keeps it as work in
progress, so a continued hypothesis builds on it.
"""

from dataclasses import dataclass

from vibesys.candidates import api as candidates
from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading
from vibesys.orchestration.single.strategy._config import SingleConfig
from vibesys.orchestration.single.strategy._state import RoundState, SingleStrategyState
from vs_core.api import InvocationRef, RevisionRef, Settle


@dataclass(frozen=True)
class _Finished:
    """The round as `candidates.settle_for` reads an attempt."""

    readings: tuple[AcceptedReading, ...]
    candidate: RevisionRef | None
    review_passed: bool | None
    failure: str | None
    judge_invocation: InvocationRef | None = None
    outcome: HypothesisOutcome | None = None


def settle_for(record: RoundState, state: SingleStrategyState, config: SingleConfig) -> Settle:
    """The assessments, eligibility and retention to propose for a finished round."""
    settled = candidates.settle_for(
        _Finished(record.readings, record.candidate, record.verdict_pass, record.failure),
        state.baseline,
        config,
    )
    if settled.retention == "discard" and record.candidate is not None and record.failure is None:
        # No trusted reading made it buildable: keep the checkpoint without making it a winner.
        return settled.model_copy(
            update={
                "retention": "candidate" if record.passed else "wip",
                "candidate": record.candidate,
                "eligible": False,
            }
        )
    return settled
