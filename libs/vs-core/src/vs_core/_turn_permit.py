"""One permit for a logical turn: its identity and the deadline it was admitted with.

Core issues the permit when the turn is dispatched. While that turn runs, the
evaluations it requests consume the permit and cannot outlive it. A non-immediate
operator Pause of this epoch admits only that turn's successors and caps them at
the permit. Outside that drain, a quota pause, a suspended resume, an
evaluation-wait continuation, a correction and a host-restart replay keep the
deadline the running run already gave them. A pause is acknowledged only when
no permit-holding work remains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from ._strategy_operations import RenderRoleArtifactsBase
from ._wait_clock import turn_bound
from .types.common import ExecuteRegisteredOperation, InvocationRef, RunStatus, Scope
from .types.evaluation import EvaluationState, SubmitMeasurement
from .types.records import RecordsState, RequestPhase, Scheduled, phase_open
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
    from collections.abc import Callable

    from .types.kernel import CoreState, RunState
    from .types.records import RequestRecord


class TurnPermit(BaseModel):
    """The admission of one logical turn.

    ``turn`` is the admitted invocation. ``deadline_at`` is that turn's bound
    (``turn_bound``: the stated limit plus any capacity-hold credit). A successor
    is capped at it only while a non-immediate Pause of this epoch is the authority.
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

    Open request records are the durable outbox (the same set as
    ``pending_requests``; a blocked record is not open and holds no permit), and a non-terminal owned job is a measurement the
    permit admitted. ``running`` is a local call that has not returned. The
    run is paused only when this is false.
    """
    if running:
        return True
    if any(phase_open(row.phase) for row in state.records.records):
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
    facts = _facts(state)

    def permit_deadline(row: RequestRecord) -> float | None:
        permit = _for_request(facts, row.request, live=False)
        return None if permit is None else permit.deadline_at

    return cap_open_records(state, permit_deadline)


def cap_open_records(
    state: CoreState, deadline_of: Callable[[RequestRecord], float | None]
) -> CoreState:
    """Cap each open record, and the agent handle that names it, at the deadline ``deadline_of`` gives.

    A record whose deadline is None, or already no later, keeps its own.
    """
    capped: dict[object, float] = {}
    records = []
    changed = False
    for row in state.records.records:
        if not phase_open(row.phase):
            records.append(row)
            continue
        deadline = deadline_of(row)
        if deadline is None or row.until.at <= deadline:
            records.append(row)
            continue
        changed = True
        capped[row.request_id] = deadline
        limit = row.limit_from_dispatch
        if limit is not None:
            limit = min(limit, max(deadline - state.run.now_at, 0.0))
        records.append(
            row.model_copy(
                update={
                    "until": row.until.model_copy(update={"at": deadline}),
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


def drain_in_force(run: RunState, records: RecordsState) -> bool:
    """Whether the run's pause still holds its authority over the work in flight.

    ``RunPark.epoch`` is the epoch the operator paused in: it never moves, so a resume is admitted
    in any later epoch (a drain a kill interrupted parks in the next epoch, and the Resume that
    follows is the relaunch). The drain's authority is a different thing: a run that is still
    draining keeps it in whatever epoch it is, and a parked run keeps it for the epoch it parked in.
    """
    park = run.park
    if park is None:
        return False
    return run.status is RunStatus.DRAINING or park.epoch == records.epoch


def _operator_drain(facts: _Facts) -> bool:
    """Whether a non-immediate operator Pause of this epoch is draining successors.

    A quota pause holds the run and sets no park. It is not this drain, and a
    running turn's own permit is not one either.
    """
    park = facts.run.park
    return park is not None and not park.immediate and drain_in_force(facts.run, facts.records)


def _for_request(facts: _Facts, request: object, *, live: bool) -> TurnPermit | None:
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return _for_turn(facts, request, live=live)
    if isinstance(request, SubmitMeasurement) and request.decision_id is None:
        return _permit_for_turn(facts, request.requested_by, live=live)
    if not _operator_drain(facts):
        return None
    if isinstance(request, EnsureSession):
        return _for_session(facts, request.spec.session_id, live=live)
    if isinstance(request, ExecuteRegisteredOperation):
        return _for_operation(facts, request, live=live)
    return None


def _for_turn(
    facts: _Facts, request: DispatchTurn | ResumeSessionTurn, *, live: bool
) -> TurnPermit | None:
    """The permit a turn consumes.

    Outside an operator Pause drain a new provider turn keeps the deadline it
    was asked with. Only an admission this invocation itself holds can cap it.
    The drain also accepts the ancestor the turn continues.
    """
    own = _own_ref(request)
    if not _operator_drain(facts):
        return _own_holding(facts, own, live=live)
    permit = _lookup(facts, own, live=live)
    if permit is not None:
        return permit
    return _lookup(facts, _turn_anchor(facts.evaluation, request.turn), live=live)


def _own_holding(facts: _Facts, ref: InvocationRef, *, live: bool) -> TurnPermit | None:
    """The permit ``ref`` itself holds. An ancestor it continues does not count."""
    invocation = _invocation(facts.sessions, ref)
    if invocation is None:
        return None
    return _admitted(facts, invocation, live=live)


def _own_ref(request: DispatchTurn | ResumeSessionTurn) -> InvocationRef:
    return InvocationRef(
        session_id=request.turn.session.session_id,
        invocation_id=request.turn.invocation_id,
        generation=request.scope.generation,
    )


def scope_in_flight(sessions: SessionsState, records: RecordsState, scope: Scope) -> bool:
    """An admitted turn of this owner has crossed core's dispatch boundary."""
    return any(
        turn.scope == scope and turn_executing(turn, records) for turn in sessions.invocations
    )


def turn_executing(turn: Invocation, records: RecordsState) -> bool:
    """Whether the turn runs and its dispatch has been authorized, so its I/O may have begun."""
    return turn.phase in (SessionPhase.EXECUTING, SessionPhase.UNKNOWN) and any(
        isinstance(row.request, DispatchTurn | ResumeSessionTurn)
        and row.request.turn == turn.turn
        and row.request.scope == turn.scope
        and row.phase != RequestPhase.PREPARED
        for row in records.records
    )


def _executing_turn(sessions: SessionsState, scope: Scope) -> InvocationRef | None:
    """The one turn running in ``scope``, or None when none is or more than one is."""
    running = [
        row.invocation
        for row in sessions.invocations
        if row.scope == scope and row.phase == SessionPhase.EXECUTING
    ]
    if len(running) != 1:
        return None
    return running[0]


def _permit_for_turn(facts: _Facts, ref: InvocationRef | None, *, live: bool) -> TurnPermit | None:
    """The permit ``ref`` consumes.

    Outside an operator Pause drain of this epoch, only that turn's own bound
    counts, hold credit included. The drain also accepts the ancestor the turn
    continues. With no turn, there is no permit.
    """
    if ref is None:
        return None
    if not _operator_drain(facts):
        return _own_holding(facts, ref, live=live)
    return _lookup(facts, ref, live=live)


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


def _admitted(facts: _Facts, invocation: Invocation, *, live: bool) -> TurnPermit | None:
    """The permit ``invocation`` holds, dated at its bound including hold credit."""
    if not _holds(facts, invocation):
        return None
    deadline = _bound(facts, invocation)
    if live and facts.run.now_at >= deadline:
        return None
    return TurnPermit(turn=invocation.invocation, deadline_at=deadline)


def _bound(facts: _Facts, invocation: Invocation) -> float:
    """``invocation``'s deadline: the dispatch's ``turn_bound``, else its stated until."""
    record = next(
        (
            row
            for row in facts.records.records
            if isinstance(row.request, DispatchTurn | ResumeSessionTurn)
            and row.request.turn == invocation.turn
            and row.request.scope == invocation.scope
            and row.phase != RequestPhase.PREPARED
        ),
        None,
    )
    if record is None:
        return invocation.until.at
    return turn_bound(record, facts.run).at


def _lookup(facts: _Facts, ref: InvocationRef | None, *, live: bool) -> TurnPermit | None:
    seen: set[InvocationRef] = set()
    while ref is not None and ref not in seen:
        seen.add(ref)
        invocation = _invocation(facts.sessions, ref)
        if invocation is None:
            return None
        permit = _admitted(facts, invocation, live=live)
        if permit is not None:
            return permit
        ref = invocation.turn.predecessor
    return None


def _invocation(sessions: SessionsState, ref: InvocationRef) -> Invocation | None:
    return next((row for row in sessions.invocations if row.invocation == ref), None)


def _holds(facts: _Facts, invocation: Invocation) -> bool:
    park = facts.run.park
    records = facts.records
    # A same-epoch Pause admits only the turns it captured, including once the
    # drain has parked. An immediate pause admits none. The dispatch record
    # below is a running turn, not a Pause.
    if park is not None and drain_in_force(facts.run, records):
        return not park.immediate and invocation.invocation in park.admitted_turns
    return any(
        isinstance(row.request, DispatchTurn | ResumeSessionTurn)
        and row.request.turn == invocation.turn
        and row.request.scope == invocation.scope
        and row.phase != RequestPhase.PREPARED
        and row.dispatched_in in (None, records.epoch)
        for row in records.records
    )
