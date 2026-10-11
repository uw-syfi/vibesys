"""Lifecycle feedback of the open slots: admission, operations, failures and settlement.

The replies of the roles are folded in `_turn_events`, the gates in `_gates`. This module folds
what core and the owners report about an attempt itself, and records what core settled; the
generation module turns settled slots into individuals in slot order.
"""

from vibesys.candidates.api import STOPPED
from vibesys.orchestration.evolve.strategy import _gates as gates
from vibesys.orchestration.evolve.strategy import _ids as ids
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._operations import (
    EvidenceReadings,
    PatchExport,
    RenderedArtifacts,
)
from vibesys.orchestration.evolve.strategy._record import awaiting, fail, put, slot_of, to_settle
from vibesys.orchestration.evolve.strategy._state import (
    EvolveStrategyState,
    SettledSlot,
    SlotState,
    WorkPhase,
)
from vs_core.api import (
    AttemptExhausted,
    AttemptReady,
    AttemptSettled,
    IntentBlocked,
    OperationResult,
    Rejected,
    RunView,
    Step,
    blocked_track,
)


def on_rejected(state: EvolveStrategyState, event: Rejected) -> EvolveStrategyState:
    """A decision core refused fails the slot awaiting it."""
    slot = awaiting(state, event.decision_id.root)
    if slot is None:
        return state
    return fail(state, slot, f"decision rejected: {event.code.value}: {event.detail}")


def on_ready(state: EvolveStrategyState, event: AttemptReady) -> EvolveStrategyState:
    """The attempt holds a slot: begin its mutation, or settle it if a stop raced."""
    slot = slot_of(state, event.attempt.attempt_id)
    if slot is None or slot.phase is not WorkPhase.START:
        return state
    slot = slot.admitted(event.attempt.generation)
    if state.stopping:
        slot = slot.model_copy(
            update={"phase": WorkPhase.SETTLE, "step": Step.NEEDED, "failure": STOPPED}
        )
    else:
        slot = slot.model_copy(
            update={"phase": WorkPhase.MUTATE, "step": Step.NEEDED, "turn": None}
        )
    return put(state, slot)


def on_operation(
    state: EvolveStrategyState, view: RunView, config: EvolveConfig, event: OperationResult
) -> EvolveStrategyState:
    """Route a registered operation's typed outcome to the slot that awaits it."""
    slot = awaiting(state, ids.decision_of_operation(event.operation_id.root))
    if slot is None:
        return state
    outcome = event.outcome
    if isinstance(outcome, RenderedArtifacts):
        return _rendered(state, slot, outcome)
    if isinstance(outcome, EvidenceReadings):
        return gates.on_interpreted(state, config, slot, outcome, view)
    if isinstance(outcome, PatchExport):
        if outcome.status != "succeeded":
            return fail(state, slot, f"patch export {outcome.status}: {outcome.detail}")
        return put(state, to_settle(slot, patch=outcome.patch))
    return state


def _rendered(
    state: EvolveStrategyState, slot: SlotState, outcome: RenderedArtifacts
) -> EvolveStrategyState:
    """The prompt is stored: the slot's turn may be asked."""
    if outcome.status != "succeeded" or not outcome.prompts or slot.turn is None:
        return fail(state, slot, f"prompt render {outcome.status}")
    turn = slot.turn.with_prompts(outcome.prompts, outcome.tool_policy)
    return put(
        state, slot.model_copy(update={"turn": turn, "step": Step.RENDERED, "awaiting": None})
    )


def on_exhausted(state: EvolveStrategyState, event: AttemptExhausted) -> EvolveStrategyState:
    """A spent budget ends the slot; core grants no automatic retry."""
    slot = slot_of(state, event.attempt.attempt_id)
    if slot is None:
        return state
    return fail(state, slot, f"attempt budget exhausted ({event.reason})")


def on_blocked(state: EvolveStrategyState, event: IntentBlocked) -> EvolveStrategyState:
    """A blocked request leaves its effect unknown: end the slot awaiting it."""
    detail = f"request blocked: {event.diagnostic}"
    slot = blocked_track((item for item in state.slots if item.phase is not WorkPhase.DONE), event)
    return state if slot is None else fail(state, slot, detail)


def on_settled(state: EvolveStrategyState, event: AttemptSettled) -> EvolveStrategyState:
    """Record what core settled; a child that passed but was not eligible is a failed one."""
    settlement = event.settlement
    slot = slot_of(state, settlement.attempt.attempt_id)
    if slot is None or slot.phase is WorkPhase.DONE:
        return state
    settled = SettledSlot(
        settlement=settlement.settlement_id,
        retained=settlement.candidate,
        eligible=settlement.eligible,
    )
    passed = slot.passed and settlement.eligible and slot.failure is None
    feedback = slot.feedback
    if slot.passed and not passed and not feedback:
        feedback = "framework: core did not accept the candidate as eligible"
    return put(
        state,
        slot.model_copy(
            update={
                "settled": settled,
                "passed": passed,
                "feedback": feedback,
                "phase": WorkPhase.DONE,
                "awaiting": None,
                "step": Step.NEEDED,
            }
        ),
    )
