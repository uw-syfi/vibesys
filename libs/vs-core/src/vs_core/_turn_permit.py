"""One permit for a logical turn: its identity and the deadline it was admitted with.

Core issues the permit when the turn is dispatched. Continuations, corrections,
evaluation submissions and evaluation-wait successors consume that same permit.
A deadline derived from it never lands later than the permit's deadline, and a
pause is acknowledged only when no permit-holding work remains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from ._strategy_operations import RenderRoleArtifactsBase
from .types.common import ExecuteRegisteredOperation, InvocationRef, RunStatus
from .types.evaluation import EvaluationState, SubmitMeasurement
from .types.records import RecordsState, RequestPhase, Scheduled
from .types.sessions import (
    DispatchTurn,
    EnsureSession,
    Invocation,
    ResumeSessionTurn,
    SessionPhase,
    SessionsState,
    TurnSpec,
)
from .types.strategy import Command, Operation, RequestTurn

if TYPE_CHECKING:
    from .types.kernel import CoreState, RunState

_LIVE = (SessionPhase.EXECUTING, SessionPhase.UNKNOWN, SessionPhase.SUSPENDED)


class TurnPermit(BaseModel):
    """The admission of one logical turn.

    ``turn`` is the admitted invocation. ``deadline_at`` is the absolute time
    that invocation was given, and every successor is capped at it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    turn: InvocationRef
    deadline_at: float


@dataclass(frozen=True)
class _Facts:
    run: RunState
    sessions: SessionsState
    records: RecordsState
    evaluation: EvaluationState


def permit_for_request(state: CoreState, request: object) -> TurnPermit | None:
    """The live permit ``request`` consumes, or None when the turn was not admitted."""
    return _for_request(_facts(state), request, live=True)


def admitted_work_remains(state: CoreState, *, running: bool = False) -> bool:
    """Whether pause must still wait for work that holds a permit.

    Unfinished request records are the durable outbox (the same set as
    ``pending_requests``), and a non-terminal owned job is a measurement the
    permit admitted. ``running`` is a local call that has not returned. The
    run is paused only when this is false.
    """
    if running:
        return True
    if any(row.phase != RequestPhase.COMPLETED for row in state.records.records):
        return True
    return any(
        not job.terminal for job in (*state.evaluation.jobs, *state.evaluation.registered_jobs)
    )


def cap_scheduled(state: CoreState, scheduled: Scheduled) -> Scheduled:
    """``scheduled`` with its deadline capped at the permit it consumes."""
    permit = _for_request(_facts(state), scheduled.request, live=False)
    return _cap_pair(state.run.now_at, scheduled, permit)


def cap_to_permits(state: CoreState) -> CoreState:
    """Cap open records and the agent handles that name them at their permit."""
    capped: dict[object, float] = {}
    records = []
    changed = False
    facts = _facts(state)
    for row in state.records.records:
        if row.phase in (RequestPhase.COMPLETED, RequestPhase.BLOCKED):
            records.append(row)
            continue
        permit = _for_request(facts, row.request, live=False)
        if permit is None or row.until.at <= permit.deadline_at:
            records.append(row)
            continue
        changed = True
        capped[row.request_id] = permit.deadline_at
        limit = row.limit_from_dispatch
        if limit is not None:
            limit = min(limit, max(permit.deadline_at - state.run.now_at, 0.0))
        records.append(
            row.model_copy(
                update={
                    "until": row.until.model_copy(update={"at": permit.deadline_at}),
                    "limit_from_dispatch": limit,
                }
            )
        )
    calls = []
    calls_changed = False
    for call in state.evaluation.agent_calls:
        deadline = capped.get(call.request_id)
        if deadline is None or call.deadline_at is None or call.deadline_at <= deadline:
            calls.append(call)
            continue
        calls_changed = True
        calls.append(call.model_copy(update={"deadline_at": deadline}))
    if not changed and not calls_changed:
        return state
    evaluation = state.evaluation
    if calls_changed:
        evaluation = evaluation.model_copy(update={"agent_calls": tuple(calls)})
    ledger = state.records
    if changed:
        ledger = ledger.model_copy(update={"records": tuple(records)})
    return state.model_copy(update={"records": ledger, "evaluation": evaluation})


def cap_dispatch_until(facts: _Facts, request: object, at: float) -> float:
    """The dispatch deadline, no later than the permit ``request`` consumes."""
    permit = _for_request(facts, request, live=False)
    if permit is None:
        return at
    return min(at, permit.deadline_at)


def facts_for(
    *,
    run: RunState,
    sessions: SessionsState,
    records: RecordsState,
    evaluation: EvaluationState,
) -> _Facts:
    """The permit's inputs when the caller has them apart from a ``CoreState``."""
    return _Facts(run, sessions, records, evaluation)


def _facts(state: CoreState) -> _Facts:
    return _Facts(state.run, state.sessions, state.records, state.evaluation)


def _cap_pair(now_at: float, scheduled: Scheduled, permit: TurnPermit | None) -> Scheduled:
    if permit is None:
        return scheduled
    until = scheduled.until
    at = min(until.at, permit.deadline_at)
    limit = scheduled.limit_from_dispatch
    if limit is not None:
        limit = min(limit, max(permit.deadline_at - now_at, 0.0))
    if at == until.at and limit == scheduled.limit_from_dispatch:
        return scheduled
    return scheduled.model_copy(
        update={"until": until.model_copy(update={"at": at}), "limit_from_dispatch": limit}
    )


def _for_request(facts: _Facts, request: object, *, live: bool) -> TurnPermit | None:
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        own = _own_ref(request)
        permit = _lookup(facts, own, live=live)
        if permit is not None:
            return permit
        return _lookup(facts, _turn_anchor(facts.evaluation, request.turn), live=live)
    if isinstance(request, SubmitMeasurement) and request.decision_id is None:
        return _for_scope(facts, request.scope, live=live)
    if isinstance(request, EnsureSession):
        return _for_session(facts, request.spec.session_id, live=live)
    if isinstance(request, ExecuteRegisteredOperation):
        return _for_operation(facts, request, live=live)
    return None


def _own_ref(request: DispatchTurn | ResumeSessionTurn) -> InvocationRef:
    return InvocationRef(
        session_id=request.turn.session.session_id,
        invocation_id=request.turn.invocation_id,
        generation=request.scope.generation,
    )


def _for_scope(facts: _Facts, scope: object, *, live: bool) -> TurnPermit | None:
    found: list[TurnPermit] = []
    for invocation in facts.sessions.invocations:
        if invocation.scope != scope or invocation.phase not in _LIVE:
            continue
        permit = _lookup(facts, invocation.invocation, live=live)
        if permit is not None:
            found.append(permit)
    if not found:
        return None
    return min(found, key=lambda item: item.deadline_at)


def _for_session(facts: _Facts, session_id: object, *, live: bool) -> TurnPermit | None:
    for invocation in facts.sessions.invocations:
        if invocation.turn.session.session_id != session_id:
            continue
        permit = _lookup(facts, invocation.turn.predecessor, live=live)
        if permit is not None:
            return permit
    return None


def _for_operation(
    facts: _Facts, request: ExecuteRegisteredOperation, *, live: bool
) -> TurnPermit | None:
    if request.request_id is None:
        return None
    receipt = next(
        (row for row in facts.run.receipts if request.request_id in row.request_ids), None
    )
    if receipt is None or receipt.decision is None:
        return None
    return _for_command(facts, receipt.decision, live=live)


def _for_command(facts: _Facts, command: Command, *, live: bool) -> TurnPermit | None:
    if isinstance(command, RequestTurn):
        return _lookup(facts, _turn_anchor(facts.evaluation, command.turn), live=live)
    if isinstance(command, Operation) and isinstance(command.request, RenderRoleArtifactsBase):
        return _lookup(facts, command.request.continuation_of, live=live)
    return None


def permit_for_command(state: CoreState, command: Command) -> TurnPermit | None:
    """The live permit a drain successor of ``command`` consumes."""
    return _for_command(_facts(state), command, live=True)


def invocation_permit(state: CoreState, ref: InvocationRef) -> TurnPermit | None:
    """The live permit of ``ref`` or the admitted ancestor it continues."""
    return _lookup(_facts(state), ref, live=True)


def _turn_anchor(evaluation: EvaluationState, turn: TurnSpec) -> InvocationRef | None:
    if turn.continuation_id is not None:
        continuation = next(
            (
                row
                for row in evaluation.continuations
                if row.continuation_id == turn.continuation_id
            ),
            None,
        )
        return None if continuation is None else continuation.invocation
    return turn.predecessor


def _lookup(facts: _Facts, ref: InvocationRef | None, *, live: bool) -> TurnPermit | None:
    seen: set[InvocationRef] = set()
    while ref is not None and ref not in seen:
        seen.add(ref)
        invocation = _invocation(facts.sessions, ref)
        if invocation is None:
            return None
        if _holds(facts, invocation):
            if live and facts.run.now_at >= invocation.until.at:
                return None
            return TurnPermit(turn=invocation.invocation, deadline_at=invocation.until.at)
        ref = invocation.turn.predecessor
    return None


def _invocation(sessions: SessionsState, ref: InvocationRef) -> Invocation | None:
    return next((row for row in sessions.invocations if row.invocation == ref), None)


def _holds(facts: _Facts, invocation: Invocation) -> bool:
    park = facts.run.park
    records = facts.records
    if (
        park is not None
        and park.epoch == records.recovery.epoch
        and facts.run.status == RunStatus.DRAINING
    ):
        return not park.immediate and invocation.invocation in park.admitted_turns
    return any(
        isinstance(row.request, DispatchTurn | ResumeSessionTurn)
        and row.request.turn == invocation.turn
        and row.request.scope == invocation.scope
        and row.phase != RequestPhase.PREPARED
        and row.dispatched_in in (None, records.recovery.epoch)
        for row in records.records
    )
