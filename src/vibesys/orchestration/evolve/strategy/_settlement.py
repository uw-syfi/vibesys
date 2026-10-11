"""What a finished slot asserts to core: the shared candidate policy plus evolve's own cases.

Candidate settlement (assessments, eligibility, retention) is `vibesys.candidates`; the judge's
verdict is its `LOCAL_VALIDATION` assessment. Evolve adds two cases of its own. A child that
passes is winner-eligible and retained as a candidate. A cold start that failed after leaving a
checkpoint keeps it as work in progress, because the next cold start builds on it. Nothing else
is retained for adoption: a failed child's tree is discarded and only its feedback survives.
"""

from dataclasses import dataclass

from vibesys.candidates import api as candidates
from vibesys.candidates.api import BaselineState
from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._state import SlotState
from vs_core.api import InvocationRef, RevisionRef, Settle


@dataclass(frozen=True)
class _Finished:
    """The slot as `candidates.settle_for` reads an attempt."""

    readings: tuple[AcceptedReading, ...]
    candidate: RevisionRef | None
    review_passed: bool | None
    failure: str | None
    judge_invocation: InvocationRef | None
    outcome: HypothesisOutcome | None = None


def settle_for(slot: SlotState, config: EvolveConfig) -> Settle:
    """The assessments, eligibility and retention to propose for a finished slot."""
    settled = candidates.settle_for(
        _Finished(
            slot.readings, slot.candidate, slot.judge_pass, slot.failure, slot.judge_invocation
        ),
        BaselineState(),
        config,
    )
    if settled.eligible and not slot.passed:
        # Every gate is satisfied in the evidence, yet the slot did not pass (for example the
        # review never produced a verdict): no independent verdict supports it.
        settled = settled.model_copy(update={"eligible": False})
    cold_start = slot.search_generation == 0
    if (
        cold_start
        and settled.retention == "discard"
        and slot.candidate is not None
        and slot.failure != candidates.STOPPED
    ):
        return settled.model_copy(
            update={"retention": "wip", "candidate": slot.candidate, "eligible": False}
        )
    return settled
