"""Moves of one slot's record shared by every kind of event.

Handlers never propose decisions; they move the slot to the phase and step whose decision
`_slots.advance` proposes next. Duplicate or late events find no matching awaiting slot and
leave the state unchanged.
"""

from vibesys.orchestration.evolve.strategy._state import (
    EvolveStrategyState,
    SlotState,
    WorkPhase,
)
from vs_core.api import AttemptId, Step


def put(state: EvolveStrategyState, slot: SlotState) -> EvolveStrategyState:
    """The state with ``slot`` replacing the slot of the same attempt."""
    return state.model_copy(
        update={
            "slots": tuple(slot if item.attempt == slot.attempt else item for item in state.slots)
        }
    )


def slot_of(state: EvolveStrategyState, attempt: AttemptId) -> SlotState | None:
    """The slot that runs this attempt."""
    return next((item for item in state.slots if item.attempt == attempt), None)


def awaiting(state: EvolveStrategyState, decision: str | None) -> SlotState | None:
    """The open slot that waits on the decision with this identity."""
    return next(
        (
            item
            for item in state.slots
            if item.phase is not WorkPhase.DONE and item.awaits(decision)
        ),
        None,
    )


def fail(state: EvolveStrategyState, slot: SlotState, reason: str) -> EvolveStrategyState:
    """End the slot with a typed failure; an admitted attempt must still settle."""
    if slot.phase is WorkPhase.DONE:
        return state
    failed = slot.failed(reason)
    if not failed.feedback:
        failed = failed.model_copy(update={"feedback": f"framework: {reason}"})
    if failed.owes_settlement:
        return put(state, failed.model_copy(update={"phase": WorkPhase.SETTLE}))
    # An attempt core never admitted has nothing to settle: its slot is refunded and the child
    # earns no individual.
    return put(state, failed.model_copy(update={"phase": WorkPhase.DONE}))


def to_settle(slot: SlotState, **update: object) -> SlotState:
    """The slot at its settle step."""
    return slot.model_copy(
        update={"phase": WorkPhase.SETTLE, "step": Step.NEEDED, "awaiting": None, **update}
    )
