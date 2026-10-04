"""Pure envelope transitions for withdrawal and atomic workstream settlement.

Strategy supplies a round proposal. Lifecycle safety, refunds, phase changes,
steer drops and completion are decided together here before the shell commits.
"""

from dataclasses import replace
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.hypothesis import transitions as hypothesis_transitions
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CompleteIntent,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    PrepareIntent,
)
from vibesys.orchestration.dynamic.lifecycle import (
    step as ledger_step,
)
from vibesys.orchestration.dynamic.models import (
    DynamicState,
    DynamicWorkstream,
    JournalEntry,
    WorkstreamPhase,
    planned_id,
)
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome, RoundRecord


class AlreadySettledError(ValueError):
    """A recorded round wins the race against withdrawal."""


class WithdrawRequested(BaseModel):
    """Close a workstream to ordinary execution before stopping its effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str
    kind: IntentKind


class SettlementProposed(BaseModel):
    """A round proposal and external cleanup acknowledgement for an owned intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str
    record: RoundRecord | None = None
    at_s: float
    drop_journal: tuple[JournalEntry, ...] = ()
    unresolved: bool = False
    retry_limit: Annotated[int, Field(ge=0)]


class InterruptedTurnReplaced(BaseModel):
    """Retained WIP and the bounded replacement charge commit together."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str
    revision: str = Field(min_length=1)
    retry_limit: Annotated[int, Field(ge=0)]


type EnvelopeEvent = WithdrawRequested | SettlementProposed | InterruptedTurnReplaced


def step(
    state: DynamicState, event: EnvelopeEvent
) -> tuple[DynamicState, tuple[LifecycleIntent, ...]]:
    """Return a new envelope with one lifecycle transition; input is immutable."""
    result = state.model_copy(deep=True)
    match event:
        case WithdrawRequested(scope_id=scope_id, kind=kind):
            if kind not in {IntentKind.PARK, IntentKind.CANCEL}:
                message = f"withdraw kind must be park or cancel: {kind}"
                raise ValueError(message)
            entries = [*result.workstreams, *result.profiles]
            item = next(item for item in entries if planned_id(item.plan) == scope_id)
            if any(record.round_number == item.sequence for record in result.search.rounds):
                message = f"workstream {scope_id!r} is already settled"
                raise AlreadySettledError(message)
            intent = LifecycleIntent(
                operation_id=f"{scope_id}/{item.sequence}/{kind.value}",
                scope_id=scope_id,
                generation=item.sequence,
                kind=kind,
            )
            result.lifecycle, _ = ledger_step(result.lifecycle, PrepareIntent(intent=intent))
            return result, (intent,)
        case SettlementProposed():
            return _settle(result, event), ()
        case InterruptedTurnReplaced():
            return _replace_interrupted(result, event)


def _replace_interrupted(
    state: DynamicState,
    event: InterruptedTurnReplaced,
) -> tuple[DynamicState, tuple[LifecycleIntent, ...]]:
    index = next(
        index
        for index, item in enumerate(state.workstreams)
        if item.hypothesis_id == event.scope_id
    )
    item = state.workstreams[index]
    intents = [
        intent
        for intent in state.lifecycle.intents.values()
        if intent.scope_id == item.hypothesis_id
        and intent.generation == item.sequence
        and intent.kind is IntentKind.INTERRUPT
        and intent.stage is not IntentStage.COMPLETED
    ]
    if not intents:
        completed = [
            intent
            for intent in state.lifecycle.intents.values()
            if intent.scope_id == item.hypothesis_id
            and intent.generation == item.sequence
            and intent.kind is IntentKind.INTERRUPT
            and intent.stage is IntentStage.COMPLETED
            and intent.resume_revision == event.revision
        ]
        if completed:
            return state, ()
        message = f"{event.scope_id}: replacement requires a pending interrupt intent"
        raise ValueError(message)
    budget = item.budget.refund_interrupted(event.retry_limit)
    if budget is None:
        message = f"{event.scope_id}: interrupted past its refund bound"
        raise ValueError(message)
    sequence = item.invocation_sequence + 1
    invocation_id = f"{item.hypothesis_id}/dynamic-implementer/invocation-{sequence}"
    replacement = LifecycleIntent(
        operation_id=invocation_id,
        scope_id=item.hypothesis_id,
        generation=item.sequence,
        kind=IntentKind.TURN,
        invocation_id=invocation_id,
    )
    state.workstreams[index] = item.model_copy(
        update={
            "phase": WorkstreamPhase.IMPLEMENTING,
            "candidate_revision": event.revision,
            "budget": budget.charge(),
            "invocation_sequence": sequence,
        }
    )
    for intent in intents:
        state.lifecycle, _ = ledger_step(
            state.lifecycle,
            CompleteIntent(operation_id=intent.operation_id, resume_revision=event.revision),
        )
    state.lifecycle, _ = ledger_step(state.lifecycle, PrepareIntent(intent=replacement))
    if state.agent is not None:
        notes = state.agent.steers.get(item.hypothesis_id, [])
        state.agent.steers[item.hypothesis_id] = [
            note.model_copy(update={"reserved_to": invocation_id})
            if note.delivered_to is None and note.dropped is None and note.reserved_to is None
            else note
            for note in notes
        ]
    return state, (replacement,)


def _settle(state: DynamicState, event: SettlementProposed) -> DynamicState:
    intent = state.lifecycle.intents[event.operation_id]
    index = next(
        index
        for index, item in enumerate(state.workstreams)
        if item.hypothesis_id == intent.scope_id and item.sequence == intent.generation
    )
    item = state.workstreams[index]
    recorded = any(record.round_number == item.sequence for record in state.search.rounds)
    if not recorded:
        if intent.kind is IntentKind.PARK:
            budget = item.budget
            if item.phase is WorkstreamPhase.IMPLEMENTING:
                budget = budget.refund_interrupted(event.retry_limit) or budget
            state.workstreams[index] = item.model_copy(
                update={"phase": WorkstreamPhase.PARKED, "budget": budget}
            )
        elif intent.kind is IntentKind.CANCEL:
            if event.record is None:
                message = "cancel settlement requires a round proposal"
                raise ValueError(message)
            if event.record.round_number != intent.generation:
                message = "settlement record.round_number must match intent.generation"
                raise ValueError(message)
            record = replace(
                event.record,
                passed=False,
                hypothesis_outcome=HypothesisOutcome.INCONCLUSIVE.value,
                hypothesis_declared_outcome=HypothesisOutcome.INCONCLUSIVE.value,
                candidate_disposition=CandidateDisposition.DISCARD.value,
                candidate_retained=False,
            )
            state.workstreams[index] = item.model_copy(update={"phase": WorkstreamPhase.CANCELLED})
            active = state.search.model_copy(
                update={"active_hypothesis_id": item.hypothesis_id}, deep=True
            )
            state.search = hypothesis_transitions.append_round(active, record, keep_active=False)
            if state.search.active_hypothesis_id is not None:
                state.search = hypothesis_transitions.finish_hypothesis(state.search)
            _drop_notes(state, item.hypothesis_id, event.drop_journal)
        else:
            message = f"settlement cannot complete {intent.kind}"
            raise ValueError(message)
    _settle_children(state, state.workstreams[index], intent)
    acknowledgement = (
        BlockIntent(operation_id=event.operation_id)
        if event.unresolved
        else CompleteIntent(operation_id=event.operation_id)
    )
    state.lifecycle, _ = ledger_step(state.lifecycle, acknowledgement)
    return state


def _settle_children(state: DynamicState, item: DynamicWorkstream, intent: LifecycleIntent) -> None:
    """Drain acknowledged child turns; fence ambiguous note delivery on park."""
    for child in tuple(state.lifecycle.intents.values()):
        if child.scope_id != item.hypothesis_id or child.generation != item.sequence:
            continue
        if (
            child.kind not in {IntentKind.TURN, IntentKind.INTERRUPT}
            or child.stage is IntentStage.COMPLETED
        ):
            continue
        notes = state.agent.steers.get(item.hypothesis_id, []) if state.agent is not None else []
        reserved = [
            note
            for note in notes
            if note.reserved_to == child.operation_id
            and note.delivered_to is None
            and note.dropped is None
        ]
        if intent.kind is IntentKind.PARK and child.stage is IntentStage.DISPATCHED and reserved:
            state.lifecycle, _ = ledger_step(
                state.lifecycle, BlockIntent(operation_id=child.operation_id)
            )
        else:
            state.lifecycle, _ = ledger_step(
                state.lifecycle,
                CompleteIntent(
                    operation_id=child.operation_id,
                    resume_revision=item.candidate_revision
                    if child.kind is IntentKind.INTERRUPT
                    else None,
                ),
            )
            if (
                intent.kind is IntentKind.PARK
                and child.stage is IntentStage.PREPARED
                and state.agent is not None
            ):
                state.agent.steers[item.hypothesis_id] = [
                    note.model_copy(update={"reserved_to": None}) if note in reserved else note
                    for note in notes
                ]


def _drop_notes(state: DynamicState, scope_id: str, journal: tuple[JournalEntry, ...]) -> None:
    if state.agent is None:
        if journal:
            message = "drop_journal requires agent state"
            raise ValueError(message)
        return
    notes = state.agent.steers.get(scope_id, [])
    pending = [note for note in notes if note.delivered_to is None and note.dropped is None]
    if len(pending) != len(journal):
        message = f"drop_journal for {scope_id!r} must contain one entry per pending steer"
        raise ValueError(message)
    state.agent.steers[scope_id] = [
        note.model_copy(update={"dropped": "workstream_settled"})
        if note.delivered_to is None and note.dropped is None
        else note
        for note in notes
    ]
    state.agent.journal.extend(journal)


__all__ = [
    "AlreadySettledError",
    "EnvelopeEvent",
    "InterruptedTurnReplaced",
    "SettlementProposed",
    "WithdrawRequested",
    "step",
]
