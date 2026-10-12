"""The open slots' decisions, from starting an attempt to settling it.

One child is one attempt: start it, run the mutator's paid turn (render, then request), review
the candidate with the judge, measure the gates the run configured, interpret the evidence, ask
the advisory profiler about a passing candidate and settle. Every role works on the attempt's own
workspace, so the judge and the profiler read the exact candidate. Each step proposes the one
decision the current phase needs and moves the slot to the awaiting step; events (see
`_slot_events`, `_turn_events` and `_gates`) move it on. Lifecycle facts such as whether a turn is
dispatched or a slot is held stay in core.
"""

from vibesys.candidates.api import STOPPED, stages
from vibesys.metrics import judged_evidence
from vibesys.orchestration.evolve.strategy import _context as context
from vibesys.orchestration.evolve.strategy import _ids as ids
from vibesys.orchestration.evolve.strategy._draft import (
    Draft,
    measurement_plan,
    operation,
    render_id,
    render_request,
    request_turn,
    turn_id,
)
from vibesys.orchestration.evolve.strategy._operations import ExportPatch, InterpretEvidence
from vibesys.orchestration.evolve.strategy._prompts import PromptContext
from vibesys.orchestration.evolve.strategy._schemas import (
    JUDGE_REPLY,
    MUTATOR_REPLY,
    PROFILER_REPLY,
)
from vibesys.orchestration.evolve.strategy._settlement import settle_for
from vibesys.orchestration.evolve.strategy._state import (
    Role,
    SlotState,
    TurnRecord,
    WorkPhase,
)
from vs_core.api import (
    Access,
    AttemptBudget,
    Measure,
    StartAttempt,
    Step,
    TurnShape,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
    attempt_scope,
    ledger_refs,
    run_scope,
    workspace_for,
)

_ROLE_OF = {
    WorkPhase.MUTATE: Role.MUTATOR,
    WorkPhase.JUDGE: Role.JUDGE,
    WorkPhase.PROFILE: Role.PROFILER,
}


def role_of(phase: WorkPhase) -> Role | None:
    """The agent role a phase talks to, or None for non-turn phases."""
    return _ROLE_OF.get(phase)


def subject_of(slot: SlotState, role: Role) -> str:
    """Decision subject of one role's turns in the slot: a fresh conversation per role."""
    return f"{slot.key}.{role.value}"


def started(slot: SlotState) -> bool:
    """Whether the slot's attempt was asked of core (or is already past that)."""
    return slot.step is not Step.NEEDED or slot.phase is not WorkPhase.START


def new_turn(slot: SlotState, role: Role) -> TurnRecord:
    """The first ask of a role's logical turn: the mutator is paid, the others free."""
    return TurnRecord(
        role=role, serial=slot.next_serial, charge="paid" if role is Role.MUTATOR else "free"
    )


def decide(draft: Draft) -> None:
    """Advance every unfinished slot by at most one decision, starting lower slots first."""
    flying = sum(
        1 for item in draft.state.slots if started(item) and item.phase is not WorkPhase.DONE
    )
    advanced: list[SlotState] = []
    for item in draft.state.slots:
        moved = advance(draft, item, may_start=flying < draft.config.max_parallelism)
        if not started(item) and started(moved) and moved.phase is not WorkPhase.DONE:
            flying += 1
        advanced.append(moved)
    draft.update(slots=tuple(advanced))


def advance(draft: Draft, slot: SlotState, *, may_start: bool = True) -> SlotState:
    """Propose the next decision for ``slot`` and return its updated record."""
    if slot.phase is WorkPhase.DONE:
        return slot
    if draft.state.stopping and slot.failure != STOPPED and slot.phase is not WorkPhase.SETTLE:
        slot = _stopped(slot)
        if slot.phase is WorkPhase.DONE:
            return slot
    if slot.step in (Step.SUSPENDED, Step.RETAINING) or (
        slot.step is Step.AWAITING and slot.phase is not WorkPhase.SETTLE
    ):
        return slot
    if slot.phase in _ROLE_OF:
        return _turn(draft, slot)
    if slot.phase is WorkPhase.START:
        return _start(draft, slot) if may_start else slot
    handlers = {
        WorkPhase.MEASURE: _measure,
        WorkPhase.INTERPRET: _interpret,
        WorkPhase.EXPORT: _export,
        WorkPhase.SETTLE: _settle,
    }
    return handlers[slot.phase](draft, slot)


def _stopped(slot: SlotState) -> SlotState:
    """An operator stop ends an unstarted slot at once and settles a started one."""
    if slot.phase is WorkPhase.START and slot.step is Step.AWAITING:
        return slot  # the start is in flight; AttemptReady will settle it
    if not slot.ready:
        return slot.model_copy(
            update={"phase": WorkPhase.DONE, "failure": STOPPED, "step": Step.NEEDED}
        )
    return slot.model_copy(
        update={
            "phase": WorkPhase.SETTLE,
            "failure": STOPPED,
            "step": Step.NEEDED,
            "awaiting": None,
        }
    )


def _start(draft: Draft, slot: SlotState) -> SlotState:
    if slot.step is not Step.NEEDED:
        return slot
    config = draft.config
    identifier = ids.decision_id("start", slot.key)
    draft.emit(
        StartAttempt(
            decision_id=identifier,
            scope=run_scope(draft.view),
            attempt_id=slot.attempt,
            item_id=ids.item_id(slot.key, 1),
            workspace=WorkspacePlan(mode=config.workspace_mode, base=slot.base),
            budget=AttemptBudget(
                admission_charge=1,
                # A child does not retry its mutation: a failed child is a failed individual.
                paid_invocation_limit=1,
                retry_limit=config.max_corrections + config.max_turn_drops,
            ),
        )
    )
    return slot.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _body(draft: Draft, slot: SlotState, role: Role) -> PromptContext:
    if role is Role.MUTATOR:
        return context.mutate_prompt(draft.state, slot, draft.config)
    if role is Role.JUDGE:
        scope = attempt_scope(draft.view, slot.attempt, slot.generation)
        return context.judge_prompt(slot, draft.config, judged_evidence(draft.view, scope))
    return context.profile_prompt(slot, draft.config)


def _turn(draft: Draft, slot: SlotState) -> SlotState:
    role = _ROLE_OF[slot.phase]
    turn = slot.turn or new_turn(slot, role)
    subject = subject_of(slot, role)
    scope = attempt_scope(draft.view, slot.attempt, slot.generation)
    if slot.step is Step.NEEDED:
        if not draft.due(turn):
            return slot.model_copy(update={"turn": turn})
        body = turn.context or _body(draft, slot, role)
        identifier = render_id(subject, turn)
        draft.emit(operation(draft, identifier, scope, render_request(subject, turn, body)))
        return slot.model_copy(
            update={
                "turn": turn,
                "step": Step.RENDERING,
                "awaiting": identifier,
                "next_serial": max(slot.next_serial, turn.serial + 1),
            }
        )
    if slot.step is not Step.RENDERED:
        return slot
    config = draft.config
    if role is Role.MUTATOR:
        shape = (
            workspace_for(scope, slot.base, config.workspace_mode),
            Access.WRITE_CANDIDATE,
            MUTATOR_REPLY,
            config.mutator_turn_seconds,
        )
    else:
        # Core attributes a reviewer's verdict to a candidate only when the turn reads that exact
        # retained revision under the read-only mode; it resolves to the attempt's live workspace.
        revision = slot.candidate or slot.base
        shape = (
            workspace_for(scope, revision, WorkspaceMode.READ_ONLY_REVISION),
            Access.READ_ONLY,
            JUDGE_REPLY if role is Role.JUDGE else PROFILER_REPLY,
            config.judge_turn_seconds if role is Role.JUDGE else config.profiler_turn_seconds,
        )
    workspace, access, schema, seconds = shape
    draft.emit(
        request_turn(
            draft,
            TurnShape(
                role=role,
                subject=subject,
                workspace=workspace,
                access=access,
                output_schema=schema,
                seconds=seconds,
            ),
            turn,
            scope,
        )
    )
    return slot.model_copy(update={"step": Step.AWAITING, "awaiting": turn_id(subject, turn)})


def _measure(draft: Draft, slot: SlotState) -> SlotState:
    if slot.step is not Step.NEEDED or slot.candidate is None:
        return slot
    identifier = ids.decision_id("measure", slot.key, slot.measurements)
    draft.emit(
        Measure(
            decision_id=identifier,
            scope=attempt_scope(draft.view, slot.attempt, slot.generation),
            plan=measurement_plan(draft, slot.candidate, "official", stages(draft.config)),
        )
    )
    return slot.model_copy(
        update={
            "step": Step.AWAITING,
            "awaiting": identifier,
            "measurements": slot.measurements + 1,
        }
    )


def _interpret(draft: Draft, slot: SlotState) -> SlotState:
    if slot.step is not Step.NEEDED:
        return slot
    refs = ledger_refs(draft.view, slot.evidence)
    if refs is None:
        return slot.model_copy(
            update={
                "phase": WorkPhase.SETTLE,
                "step": Step.NEEDED,
                "awaiting": None,
                "failure": slot.failure or "the evidence is missing from core's ledger",
                "feedback": slot.feedback
                or "framework: the evidence is missing from core's ledger",
            }
        )
    identifier = ids.decision_id("interpret", slot.key)
    draft.emit(
        operation(
            draft,
            identifier,
            attempt_scope(draft.view, slot.attempt, slot.generation),
            InterpretEvidence(evidence=refs),
        )
    )
    return slot.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _export(draft: Draft, slot: SlotState) -> SlotState:
    if slot.step is not Step.NEEDED or slot.candidate is None:
        return slot
    identifier = ids.decision_id("export", slot.key)
    draft.emit(
        operation(
            draft,
            identifier,
            attempt_scope(draft.view, slot.attempt, slot.generation),
            ExportPatch(revision=slot.candidate),
        )
    )
    return slot.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _settle(draft: Draft, slot: SlotState) -> SlotState:
    if slot.settle_sent:
        return slot
    identifier = ids.decision_id("settle", slot.key)
    draft.emit(
        Withdraw(
            decision_id=identifier,
            scope=run_scope(draft.view),
            target=ids.attempt_ref(slot.attempt, slot.generation),
            disposition=settle_for(slot, draft.config),
        )
    )
    return slot.model_copy(
        update={"step": Step.AWAITING, "awaiting": identifier, "settle_sent": True}
    )
