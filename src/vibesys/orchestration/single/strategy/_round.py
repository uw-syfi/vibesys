"""The open round's decisions, from starting its attempt to settling it.

One round is one attempt: start it, run the combined implementer turn (render, then request),
measure the gates the cadence asks for, interpret the evidence and settle. Each step proposes
the one decision the current phase needs and moves the record to the awaiting step; events (see
`_round_events`) move it on. Lifecycle facts such as whether a turn is dispatched or a slot is
held stay in core.
"""

from vibesys.candidates.api import STOPPED, stages
from vibesys.orchestration.single.strategy import _context as context
from vibesys.orchestration.single.strategy import _ids as ids
from vibesys.orchestration.single.strategy._draft import (
    Draft,
    measurement_plan,
    operation,
    render_id,
    render_request,
    request_turn,
    turn_id,
)
from vibesys.orchestration.single.strategy._operations import InterpretEvidence
from vibesys.orchestration.single.strategy._schemas import IMPLEMENTER_REPLY
from vibesys.orchestration.single.strategy._settlement import settle_for
from vibesys.orchestration.single.strategy._state import (
    Role,
    RoundState,
    Step,
    TurnRecord,
    WorkPhase,
)
from vs_core.api import (
    Access,
    AttemptBudget,
    Measure,
    StartAttempt,
    TurnShape,
    Withdraw,
    WorkspacePlan,
    attempt_scope,
    ledger_refs,
    run_scope,
    workspace_for,
)

SUBJECT = "implementer"


def subject_of(record: RoundState) -> str:
    """Decision subject of the round's implementer turns."""
    return f"{record.hypothesis_id}.{record.number}.{SUBJECT}"


def key_of(record: RoundState) -> str:
    """Stable raw subject of the round; the id constructors encode it."""
    return f"{record.hypothesis_id}.{record.number}"


def decide(draft: Draft) -> None:
    """Advance the open round by at most one decision."""
    record = draft.state.round
    if record is None:
        return
    draft.update(round=advance(draft, record))


def advance(draft: Draft, record: RoundState) -> RoundState:
    """Propose the next decision for ``record`` and return its updated record."""
    if record.phase is WorkPhase.DONE:
        return record
    if draft.state.stopping and record.failure != STOPPED and record.phase is not WorkPhase.SETTLE:
        record = _stopped(record)
        if record.phase is WorkPhase.DONE:
            return record
    if record.step in (Step.SUSPENDED, Step.RETAINING) or (
        record.step is Step.AWAITING and record.phase is not WorkPhase.SETTLE
    ):
        return record
    handlers = {
        WorkPhase.START: _start,
        WorkPhase.IMPLEMENT: _turn,
        WorkPhase.MEASURE: _measure,
        WorkPhase.INTERPRET: _interpret,
        WorkPhase.SETTLE: _settle,
    }
    return handlers[record.phase](draft, record)


def _stopped(record: RoundState) -> RoundState:
    """An operator stop ends an unstarted round at once and settles a started one."""
    if record.phase is WorkPhase.START and record.step is Step.AWAITING:
        return record  # the start is in flight; AttemptReady will settle it
    if not record.ready:
        return record.model_copy(
            update={"phase": WorkPhase.DONE, "failure": STOPPED, "step": Step.NEEDED}
        )
    return record.model_copy(
        update={
            "phase": WorkPhase.SETTLE,
            "failure": STOPPED,
            "step": Step.NEEDED,
            "awaiting": None,
        }
    )


def _start(draft: Draft, record: RoundState) -> RoundState:
    if record.step is not Step.NEEDED:
        return record
    config = draft.config
    identifier = ids.decision_id("start", key_of(record))
    draft.emit(
        StartAttempt(
            decision_id=identifier,
            scope=run_scope(draft.view),
            attempt_id=record.attempt,
            item_id=ids.item_id(record.hypothesis_id, record.number),
            workspace=WorkspacePlan(mode=config.workspace_mode, base=record.base),
            budget=AttemptBudget(
                admission_charge=1,
                paid_invocation_limit=config.max_retries_per_round,
                retry_limit=config.max_corrections + config.max_turn_drops,
            ),
        )
    )
    return record.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _turn(draft: Draft, record: RoundState) -> RoundState:
    turn = record.turn or TurnRecord(
        role=Role.IMPLEMENTER, serial=record.next_serial, charge="paid"
    )
    subject = subject_of(record)
    scope = attempt_scope(draft.view, record.attempt, record.generation)
    if record.step is Step.NEEDED:
        if not draft.due(turn):
            return record
        body = turn.context or context.round_prompt(record, draft.config)
        identifier = render_id(subject, turn)
        draft.emit(operation(draft, identifier, scope, render_request(subject, turn, body)))
        return record.model_copy(
            update={
                "turn": turn,
                "step": Step.RENDERING,
                "awaiting": identifier,
                "next_serial": max(record.next_serial, turn.serial + 1),
            }
        )
    if record.step is not Step.RENDERED:
        return record
    draft.emit(
        request_turn(
            draft,
            TurnShape(
                role=Role.IMPLEMENTER,
                subject=subject,
                workspace=workspace_for(scope, record.base, draft.config.workspace_mode),
                access=Access.WRITE_CANDIDATE,
                output_schema=IMPLEMENTER_REPLY,
                seconds=draft.config.implementer_turn_seconds,
            ),
            turn,
            scope,
        )
    )
    return record.model_copy(
        update={
            "step": Step.AWAITING,
            "awaiting": turn_id(subject, turn),
            "turns_spent": record.turns_spent + (1 if turn.charge == "paid" else 0),
        }
    )


def _measure(draft: Draft, record: RoundState) -> RoundState:
    if record.step is not Step.NEEDED or record.candidate is None:
        return record
    identifier = ids.decision_id("measure", key_of(record), record.measurements)
    draft.emit(
        Measure(
            decision_id=identifier,
            scope=attempt_scope(draft.view, record.attempt, record.generation),
            plan=measurement_plan(draft, record.candidate, "official", stages(draft.config)),
        )
    )
    return record.model_copy(
        update={
            "step": Step.AWAITING,
            "awaiting": identifier,
            "measurements": record.measurements + 1,
        }
    )


def _interpret(draft: Draft, record: RoundState) -> RoundState:
    if record.step is not Step.NEEDED:
        return record
    refs = ledger_refs(draft.view, record.evidence)
    if refs is None:
        return record.model_copy(
            update={
                "phase": WorkPhase.SETTLE,
                "step": Step.NEEDED,
                "awaiting": None,
                "failure": record.failure or "the evidence is missing from core's ledger",
            }
        )
    identifier = ids.decision_id("interpret", key_of(record))
    draft.emit(
        operation(
            draft,
            identifier,
            attempt_scope(draft.view, record.attempt, record.generation),
            InterpretEvidence(evidence=refs),
        )
    )
    return record.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _settle(draft: Draft, record: RoundState) -> RoundState:
    if record.settle_sent:
        return record
    identifier = ids.decision_id("settle", key_of(record))
    draft.emit(
        Withdraw(
            decision_id=identifier,
            scope=run_scope(draft.view),
            target=ids.attempt_ref(record.attempt, record.generation),
            disposition=settle_for(record, draft.state, draft.config),
        )
    )
    return record.model_copy(
        update={"step": Step.AWAITING, "awaiting": identifier, "settle_sent": True}
    )
