"""Moves of the open round's record shared by every kind of event.

Handlers never propose decisions; they move the record to the phase and step whose decision
`_round.advance` proposes next. Duplicate or late events find no matching awaiting record and
leave the state unchanged.
"""

from dataclasses import dataclass

from vibesys.orchestration.multi.strategy._config import MultiConfig
from vibesys.orchestration.multi.strategy._prompts import ArchiveConflictView, ValidationFailure
from vibesys.orchestration.multi.strategy._state import (
    MultiStrategyState,
    Role,
    RoundState,
    Step,
    TurnRecord,
    WorkPhase,
)


def put(state: MultiStrategyState, record: RoundState) -> MultiStrategyState:
    """The state with ``record`` as its open round."""
    return state.model_copy(update={"round": record})


def awaiting(state: MultiStrategyState, decision: str | None) -> RoundState | None:
    """The open round, when it waits on the decision with this identity."""
    record = state.round
    if record is not None and record.phase is not WorkPhase.DONE and record.awaits(decision):
        return record
    return None


def fail(state: MultiStrategyState, record: RoundState, reason: str) -> MultiStrategyState:
    """End the round with a typed failure; an admitted attempt must still settle."""
    if record.phase is WorkPhase.DONE:
        return state
    failed = record.failed(reason)
    if failed.owes_settlement:
        return put(state, failed.model_copy(update={"phase": WorkPhase.SETTLE}))
    # An attempt core never admitted has nothing to settle: its slot is refunded and the
    # round ends done; `retire` then counts it as failed in the ledger.
    return put(state, failed.model_copy(update={"phase": WorkPhase.DONE}))


def to_settle(record: RoundState, **update: object) -> RoundState:
    """The record at its settle step."""
    return record.model_copy(
        update={"phase": WorkPhase.SETTLE, "step": Step.NEEDED, "awaiting": None, **update}
    )


@dataclass(frozen=True)
class Repair:
    """What the next implementer turn is asked to repair after a failed attempt."""

    feedback: str | None = None
    validation_failure: ValidationFailure | None = None
    conflict: ArchiveConflictView | None = None

    def fields(self) -> dict[str, object]:
        """The record fields that carry the repair into the next prompt."""
        return {
            "feedback": self.feedback,
            "validation_failure": self.validation_failure,
            "conflict": self.conflict,
        }


def next_attempt(record: RoundState, repair: Repair) -> RoundState:
    """The record ready for another paid implementer turn, carrying what to repair."""
    return record.model_copy(
        update={
            **repair.fields(),
            "phase": WorkPhase.IMPLEMENT,
            "step": Step.NEEDED,
            "awaiting": None,
            "evidence": (),
            "readings": (),
            "judge_pass": None,
            "judge_invocation": None,
            "review_skipped": False,
            "validated": False,
            "official_reason": None,
            "turn": TurnRecord(role=Role.IMPLEMENTER, serial=record.next_serial, charge="paid"),
        }
    )


def retry_or_settle(
    state: MultiStrategyState, config: MultiConfig, record: RoundState, repair: Repair
) -> MultiStrategyState:
    """After a failed attempt: another paid turn within the budget, else settle the round."""
    if record.turns_spent >= config.max_retries_per_round:
        return put(state, to_settle(record, **repair.fields()))
    return put(state, next_attempt(record, repair))
