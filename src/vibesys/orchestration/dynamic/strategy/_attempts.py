"""Workstream decisions, from parent verification to settlement.

Each step verifies the parent, starts the attempt, runs a role's turn, measures,
interprets or settles.

Each attempt is a small state machine over `WorkPhase` and `Step`. `advance`
proposes the one decision the current phase needs and moves the record to the
awaiting step; events (see `_attempt_events`) move it on. Lifecycle facts such as
whether a turn is dispatched or a slot is held stay in core.
"""

from vibesys.orchestration.dynamic.strategy import _context as context
from vibesys.orchestration.dynamic.strategy import _ids as ids
from vibesys.orchestration.dynamic.strategy._baseline import stages
from vibesys.orchestration.dynamic.strategy._draft import (
    Draft,
    attempt_scope,
    measurement_plan,
    operation,
    render_id,
    render_request,
    request_turn,
    run_scope,
    turn_id,
    workspace_for,
)
from vibesys.orchestration.dynamic.strategy._operations import (
    InterpretEvidence,
    VerifyParentRevision,
)
from vibesys.orchestration.dynamic.strategy._prompts import PromptContext
from vibesys.orchestration.dynamic.strategy._schemas import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PROFILER_REPLY,
)
from vibesys.orchestration.dynamic.strategy._settlement import STOPPED, settle_for
from vibesys.orchestration.dynamic.strategy._state import (
    AttemptRecord,
    Role,
    Step,
    TurnRecord,
    WorkKind,
    WorkPhase,
)
from vs_core.api import (
    Access,
    AttemptBudget,
    Measure,
    StartAttempt,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
)

_TURN_PHASES = {
    WorkPhase.IMPLEMENT: Role.IMPLEMENTER,
    WorkPhase.PROFILE: Role.PROFILER,
    WorkPhase.REVIEW: Role.JUDGE,
}


def key_of(record: AttemptRecord) -> str:
    """Stable subject of one workstream."""
    return f"{record.plan.work_id}.{record.sequence}"


def subject_of(record: AttemptRecord, role: Role) -> str:
    """Decision subject of one role's turns inside a workstream."""
    return f"{key_of(record)}.{role.value}"


def role_of(phase: WorkPhase) -> Role | None:
    """The agent role a phase talks to, or None for non-turn phases."""
    return _TURN_PHASES.get(phase)


def _first_turn(role: Role) -> TurnRecord:
    return TurnRecord(role=role, serial=0, charge="free" if role is Role.JUDGE else "paid")


def decide(draft: Draft) -> None:
    """Advance every unfinished workstream by at most one decision."""
    draft.update(attempts=tuple(advance(draft, item) for item in draft.state.attempts))


def advance(draft: Draft, record: AttemptRecord) -> AttemptRecord:
    """Propose the next decision for ``record`` and return its updated record."""
    if record.phase is WorkPhase.DONE:
        return record
    if draft.state.stopping and record.failure != STOPPED and record.phase is not WorkPhase.SETTLE:
        record = _stopped(record)
        if record.phase is WorkPhase.DONE:
            return record
    if record.step is Step.SUSPENDED or (
        record.step is Step.AWAITING and record.phase is not WorkPhase.SETTLE
    ):
        return record
    if record.phase in _TURN_PHASES:
        return _turn(draft, record)
    handlers = {
        WorkPhase.VERIFY_PARENT: _verify,
        WorkPhase.START: _start,
        WorkPhase.MEASURE: _measure,
        WorkPhase.INTERPRET: _interpret,
        WorkPhase.SETTLE: _settle,
    }
    return handlers[record.phase](draft, record)


def _stopped(record: AttemptRecord) -> AttemptRecord:
    """An operator stop ends unstarted work at once and settles started work."""
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


def _verify(draft: Draft, record: AttemptRecord) -> AttemptRecord:
    if record.step is not Step.NEEDED:
        return record
    identifier = ids.decision_id("verify", key_of(record))
    draft.emit(
        operation(
            draft, identifier, run_scope(draft.view), VerifyParentRevision(parent=record.parent)
        )
    )
    return record.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _start(draft: Draft, record: AttemptRecord) -> AttemptRecord:
    if record.step is not Step.NEEDED:
        return record
    identifier = ids.decision_id("start", key_of(record))
    verified = record.parent != draft.view.facts.baseline
    draft.emit(
        StartAttempt(
            decision_id=identifier,
            scope=run_scope(draft.view),
            depends_on=(ids.decision_id("verify", key_of(record)),) if verified else (),
            attempt_id=record.attempt,
            item_id=ids.item_id(record.plan.work_id, record.sequence),
            workspace=WorkspacePlan(
                mode=(
                    WorkspaceMode.ISOLATED_CHILD
                    if record.plan.kind is WorkKind.IMPLEMENT
                    else WorkspaceMode.READ_ONLY_REVISION
                ),
                base=record.parent,
            ),
            budget=AttemptBudget(
                admission_charge=1, paid_invocation_limit=draft.config.max_retries_per_round
            ),
        )
    )
    return record.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _default_context(record: AttemptRecord, draft: Draft) -> PromptContext:
    if record.phase is WorkPhase.IMPLEMENT:
        return context.implement_prompt(record, draft.state)
    if record.phase is WorkPhase.PROFILE:
        return context.profile_prompt(record)
    return context.review_prompt(record)


def _turn(draft: Draft, record: AttemptRecord) -> AttemptRecord:
    role = _TURN_PHASES[record.phase]
    turn = record.turn or _first_turn(role)
    subject = subject_of(record, role)
    scope = attempt_scope(draft.view, record.attempt, record.generation)
    if record.step is Step.NEEDED:
        body = turn.context or _default_context(record, draft)
        identifier = render_id(subject, turn)
        draft.emit(operation(draft, identifier, scope, render_request(subject, turn, body)))
        return record.model_copy(
            update={"turn": turn, "step": Step.RENDERING, "awaiting": identifier}
        )
    if record.step is not Step.RENDERED:
        return record
    mode, access, schema, seconds, revision = _turn_shape(draft, record, role)
    draft.emit(
        request_turn(
            draft,
            role=role,
            subject=subject,
            turn=turn,
            scope=scope,
            workspace=workspace_for(scope, revision, mode),
            access=access,
            reuse=(record.plan.continue_hypothesis and role is Role.IMPLEMENTER) or turn.serial > 0,
            output_schema=schema,
            seconds=seconds,
        )
    )
    return record.model_copy(
        update={
            "step": Step.AWAITING,
            "awaiting": turn_id(subject, turn),
            "turns_spent": record.turns_spent + (1 if turn.charge == "paid" else 0),
        }
    )


def _turn_shape(draft: Draft, record: AttemptRecord, role: Role):  # noqa: ANN202
    config = draft.config
    if role is Role.IMPLEMENTER:
        return (
            WorkspaceMode.ISOLATED_CHILD,
            Access.WRITE_CANDIDATE,
            IMPLEMENTER_REPLY,
            config.implementer_turn_seconds,
            record.parent,
        )
    if role is Role.PROFILER:
        return (
            WorkspaceMode.READ_ONLY_REVISION,
            Access.WRITE_ARTIFACTS,
            PROFILER_REPLY,
            config.profiler_turn_seconds,
            record.parent,
        )
    return (
        WorkspaceMode.READ_ONLY_REVISION,
        Access.READ_ONLY,
        JUDGE_REPLY,
        config.judge_turn_seconds,
        record.candidate or record.parent,
    )


def _measure(draft: Draft, record: AttemptRecord) -> AttemptRecord:
    if record.step is not Step.NEEDED:
        return record
    profile = record.plan.kind is WorkKind.PROFILE
    candidate = record.parent if profile or record.candidate is None else record.candidate
    identifier = ids.decision_id("measure", key_of(record))
    draft.emit(
        Measure(
            decision_id=identifier,
            scope=attempt_scope(draft.view, record.attempt, record.generation),
            plan=measurement_plan(
                draft,
                candidate,
                "profile" if profile else "official",
                ("profile",) if profile else stages(draft.config),
            ),
        )
    )
    return record.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _interpret(draft: Draft, record: AttemptRecord) -> AttemptRecord:
    if record.step is not Step.NEEDED:
        return record
    identifier = ids.decision_id("interpret", key_of(record))
    draft.emit(
        operation(
            draft,
            identifier,
            attempt_scope(draft.view, record.attempt, record.generation),
            InterpretEvidence(evidence=record.evidence),
        )
    )
    return record.model_copy(update={"step": Step.AWAITING, "awaiting": identifier})


def _settle(draft: Draft, record: AttemptRecord) -> AttemptRecord:
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
