"""The open round's decisions, from starting its attempt to settling it.

One round is one attempt: start it, run the implementer's turn (render, then request), review
it with the judge when the cadence says so, run the implementer's validation recipes, measure the
gates the cadence asks for, interpret the evidence and settle. Every role works on the attempt's
own workspace, so the judge reads the exact candidate. Each step proposes the one decision the
current phase needs and moves the record to the awaiting step; events (see `_round_events`,
`_turn_events` and `_gates`) move it on. Lifecycle facts such as whether a turn is dispatched or
a slot is held stay in core.
"""

from vibesys.candidates.api import STOPPED, stages
from vibesys.orchestration.multi.strategy import _context as context
from vibesys.orchestration.multi.strategy import _ids as ids
from vibesys.orchestration.multi.strategy._draft import (
    Draft,
    measurement_plan,
    operation,
    render_id,
    render_request,
    request_turn,
    turn_id,
)
from vibesys.orchestration.multi.strategy._operations import InterpretEvidence, ValidateLocal
from vibesys.orchestration.multi.strategy._prompts import PromptContext
from vibesys.orchestration.multi.strategy._schemas import IMPLEMENTER_REPLY, JUDGE_REPLY
from vibesys.orchestration.multi.strategy._settlement import settle_for
from vibesys.orchestration.multi.strategy._state import (
    Role,
    RoundState,
    Step,
    TurnRecord,
    WorkPhase,
)
from vibesys.serial_search import api as serial
from vs_core.api import (
    Access,
    AttemptBudget,
    Measure,
    Scope,
    StartAttempt,
    TurnShape,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
    WorkspaceRef,
    attempt_scope,
    ledger_refs,
    run_scope,
    workspace_for,
)

_ROLE_OF = {WorkPhase.IMPLEMENT: Role.IMPLEMENTER, WorkPhase.REVIEW: Role.JUDGE}


def role_of(phase: WorkPhase) -> Role | None:
    """The agent role a phase talks to, or None for non-turn phases."""
    return _ROLE_OF.get(phase)


def subject_of(record: RoundState, role: Role) -> str:
    """Decision subject of one role's turns in the round.

    The implementer keeps one conversation for the round's retries; each review is a fresh one,
    named by the paid turns spent when it starts, so the judge never sees an earlier verdict.
    """
    if role is Role.JUDGE:
        return f"{key_of(record)}.judge.{record.turns_spent}"
    return f"{key_of(record)}.{role.value}"


def key_of(record: RoundState) -> str:
    """Stable raw subject of the round; the id constructors encode it."""
    return f"{record.hypothesis_id}.{record.number}"


def new_turn(record: RoundState, role: Role) -> TurnRecord:
    """The first ask of a role's logical turn: the judge is free, the implementer paid."""
    return TurnRecord(
        role=role, serial=record.next_serial, charge="free" if role is Role.JUDGE else "paid"
    )


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
    if record.phase in _ROLE_OF:
        return _turn(draft, record)
    handlers = {
        WorkPhase.START: _start,
        WorkPhase.VALIDATE: _validate,
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


def _body(draft: Draft, record: RoundState, role: Role) -> PromptContext:
    ledger = context.ledger_of(draft.state, draft.config)
    hypothesis = ledger.search.active_hypothesis
    if role is Role.IMPLEMENTER:
        focus = serial.focus_of(ledger, serial.focus_policy(draft.config.attribution))
        return context.implement_prompt(record, draft.config, hypothesis, focus)
    reply = context.response_of(record)
    if reply is None:
        message = "a review needs the implementer's reply"
        raise RuntimeError(message)
    return context.judge_prompt(record, draft.config, hypothesis, reply)


def _turn(draft: Draft, record: RoundState) -> RoundState:
    role = _ROLE_OF[record.phase]
    turn = record.turn or new_turn(record, role)
    subject = subject_of(record, role)
    scope = attempt_scope(draft.view, record.attempt, record.generation)
    if record.step is Step.NEEDED:
        if not draft.due(turn):
            return record.model_copy(update={"turn": turn})
        body = turn.context or _body(draft, record, role)
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
    config = draft.config
    implementing = role is Role.IMPLEMENTER
    draft.emit(
        request_turn(
            draft,
            TurnShape(
                role=role,
                subject=subject,
                workspace=_workspace(record, config.workspace_mode, scope, role),
                access=Access.WRITE_CANDIDATE if implementing else Access.READ_ONLY,
                output_schema=IMPLEMENTER_REPLY if implementing else JUDGE_REPLY,
                seconds=(
                    config.implementer_turn_seconds if implementing else config.judge_turn_seconds
                ),
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


def _workspace(record: RoundState, mode: WorkspaceMode, scope: Scope, role: Role) -> WorkspaceRef:
    """The workspace a turn works in: the attempt's, or for a review a read-only view of the candidate.

    Core attributes a reviewer's verdict to the candidate only when the review reads that exact
    retained revision under the read-only mode, so a review of a retained candidate names it.
    Both resolve to the attempt's one live workspace; the review's access is read-only.
    """
    if role is Role.JUDGE and record.candidate is not None:
        return workspace_for(scope, record.candidate, WorkspaceMode.READ_ONLY_REVISION)
    return workspace_for(scope, record.base, mode)


def _validate(draft: Draft, record: RoundState) -> RoundState:
    """Ask for the trusted run of the implementer's validation recipes at the candidate."""
    reply = context.response_of(record)
    recipe = None if reply is None else reply.validation_recipe_artifact
    if record.step is not Step.NEEDED or record.candidate is None or recipe is None:
        return record
    identifier = ids.decision_id("validate", key_of(record), record.turns_spent)
    draft.emit(
        operation(
            draft,
            identifier,
            attempt_scope(draft.view, record.attempt, record.generation),
            ValidateLocal(revision=record.candidate, recipe_artifact=recipe),
        )
    )
    return record.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


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
