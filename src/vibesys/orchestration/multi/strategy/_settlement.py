"""What a finished round asserts to core: multi's review cases over the shared policy.

Candidate settlement (assessments, eligibility, retention) is `vibesys.candidates`; the judge's
verdict is its `LOCAL_VALIDATION` assessment. Multi adds the cases of its own. A round whose
review was deferred by the sparse policy or never reached produces no verdict, so it can never be
adopted; a round that passed review but was not measured retains its checkpoint as a buildable
parent (`candidate`, ineligible); a round that failed after producing a checkpoint keeps it as work
in progress, so a continued hypothesis builds on it.
"""

from dataclasses import dataclass

from vibesys.candidates import api as candidates
from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading
from vibesys.orchestration.multi.strategy import _context as context
from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._state import MultiStrategyState, RoundState
from vs_core.api import InvocationRef, RevisionRef, Settle


@dataclass(frozen=True)
class _Finished:
    """The round as `candidates.settle_for` reads an attempt."""

    readings: tuple[AcceptedReading, ...]
    candidate: RevisionRef | None
    review_passed: bool | None
    failure: str | None
    judge_invocation: InvocationRef | None
    outcome: HypothesisOutcome | None


def settle_for(record: RoundState, state: MultiStrategyState, config: MultiConfig) -> Settle:
    """The assessments, eligibility and retention to propose for a finished round."""
    reply = context.response_of(record)
    settled = candidates.settle_for(
        _Finished(
            record.readings,
            record.candidate,
            record.judge_pass,
            record.failure,
            record.judge_invocation,
            None if reply is None else reply.hypothesis_outcome,
        ),
        state.baseline,
        config,
    )
    if settled.eligible and record.judge_pass is not True:
        # No independent verdict supports this candidate, so it cannot be adopted.
        settled = settled.model_copy(update={"eligible": False})
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
