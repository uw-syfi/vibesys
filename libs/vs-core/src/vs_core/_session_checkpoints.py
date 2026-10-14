"""Checkpoint authority and deferred suspension publication for Sessions.

advance consumes canonical terminal yields and retained checkpoint observations.
It owns run checkpoint receipts and checkpoint-backed invocation/session updates;
Inputs and Turns never need the pending-publication mechanics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import (
    Proven,
    current_admission,
    fresh_observation,
    invocation_for,
    observation_for,
    request_matches,
)
from ._session_rows import awaiting as _awaiting
from ._session_rows import replace_invocation as _replace_invocation
from ._session_rows import replace_session as _replace_session
from ._session_rows import session_row as _session
from ._session_scope import attempt_for, proven_invocation, scope_active
from ._waits import awaiting_refund, draining_without_cancel, stalled_checkpoint_waits
from .types.attempts import SnapshotAndRetain
from .types.common import (
    AttemptId,
    AttemptRef,
    CheckpointDecline,
    CompletionStatus,
    ContractValidationError,
    LifecycleClass,
    ObservationStatus,
    RequestId,
)
from .types.evaluation import TurnSuspended
from .types.kernel import AreaChange, DecisionCompleted
from .types.records import ExecuteRegisteredOperation, RequestPhase, within
from .types.sessions import (
    CheckpointFailure,
    CheckpointStalled,
    DispatchTurn,
    InspectTurn,
    InterruptFailed,
    InvocationCheckpointAvailable,
    ResumeSessionTurn,
    RunInvocationCheckpoint,
    RunInvocationCheckpointObserved,
    RunInvocationCheckpointRequested,
    SessionPhase,
    SnapshotAndRetainRun,
    YieldPublicationFailed,
)

if TYPE_CHECKING:
    from .types.common import InvocationRef, Observation
    from .types.kernel import SessionsContext, Signal
    from .types.records import RequestRecord
    from .types.sessions import Invocation, SessionsEvent, SessionsState


def _record(context: SessionsContext, identity: RequestId | None) -> RequestRecord | None:
    return context.records.record(identity)


def _terminal(invocation: Invocation) -> bool:
    obs = invocation.observation
    return (
        obs is not None
        and obs.terminal
        and obs.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def turn_source_matches(
    context: SessionsContext, invocation: Invocation, observation: Observation
) -> bool:
    record = _record(context, observation.request_id)
    if (
        record is None
        or record.request.scope != invocation.scope
        or not isinstance(
            invocation_for((invocation,), invocation.invocation, invocation.scope), Proven
        )
        or not isinstance(request_matches(record, record.request), Proven)
        or not isinstance(observation_for(record, observation), Proven)
    ):
        return False
    request = record.request
    if isinstance(request, InspectTurn):
        matches = request.invocation == invocation.invocation
    elif isinstance(request, DispatchTurn | ResumeSessionTurn):
        matches = request.turn == invocation.turn
    else:
        matches = (
            isinstance(request, ExecuteRegisteredOperation)
            and record.lifecycle == LifecycleClass.SESSION_TURN
            and request.operation_id == invocation.registered_operation
            and invocation.registered_operation is not None
        )
    return matches and (
        not isinstance(invocation.scope.owner, AttemptId)
        or isinstance(
            current_admission(
                attempt_for(context, invocation.scope), invocation.scope, observation.admission_id
            ),
            Proven,
        )
    )


def _turn_id(ref: InvocationRef, action: str) -> RequestId:
    return RequestId(
        root=f"run-checkpoint:{len(ref.session_id.root)}:{ref.session_id.root}:{ref.generation}:"
        f"{len(ref.invocation_id.root)}:{ref.invocation_id.root}:{action}"
    )


def _yield_checkpoint_completion(
    context: SessionsContext, invocation: Invocation, event: InvocationCheckpointAvailable
) -> tuple[Signal, ...]:
    observation = invocation.observation
    if (
        invocation.phase != SessionPhase.SUSPENDED
        or event.retention != "wip"
        or observation is None
        or not observation.accepted
        or observation.status != ObservationStatus.SUCCEEDED
        or not turn_source_matches(context, invocation, observation)
    ):
        return ()
    record = _record(context, observation.request_id)
    if record is None or record.request.decision_id is None:
        return ()
    decision_id = record.request.decision_id
    receipt = next((row for row in context.run.receipts if row.decision_id == decision_id), None)
    if receipt is None or receipt.completion is not None:
        return ()
    return (DecisionCompleted(decision_id=decision_id, status=CompletionStatus.SUCCEEDED),)


def checkpoint_matches(
    state: SessionsState, context: SessionsContext, event: InvocationCheckpointAvailable
) -> bool:
    """Prove one retained receipt with canonical snapshot payload and terminal source."""
    invocation = proven_invocation(state, event.invocation)
    record = _record(context, event.request_id)
    if (
        invocation is None
        or not _terminal(invocation)
        or invocation.observation is None
        or not turn_source_matches(context, invocation, invocation.observation)
        or record is None
        or not isinstance(request_matches(record, record.request), Proven)
        or record.request.scope != invocation.scope
    ):
        return False
    owner = attempt_for(context, invocation.scope)
    request = record.request
    if owner is None:
        if not isinstance(request, SnapshotAndRetainRun) or request.invocation != event.invocation:
            return False
        proofs = state.run_checkpoints
    else:
        if (
            not isinstance(request, SnapshotAndRetain)
            or request.invocation != event.invocation
            or request.attempt
            != AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
            or not isinstance(
                current_admission(owner, invocation.scope, request.admission_id), Proven
            )
        ):
            return False
        proofs = owner.checkpoints
    rows = tuple(row for row in proofs if row.request_id == event.request_id)
    return (
        len(rows) == 1
        and rows[0].invocation == event.invocation
        and rows[0].revision == event.revision
        and rows[0].retention == event.retention == request.retention
    )


def _publication_ready(
    state: SessionsState, context: SessionsContext, invocation: Invocation
) -> bool:
    session = _session(state, invocation.invocation.session_id)
    observation = invocation.observation
    return (
        session is not None
        and session.spec == invocation.turn.session
        and session.scope == invocation.scope
        and session.generation == invocation.invocation.generation
        and session.invocation == invocation.invocation.invocation_id
        and session.accepted
        and session.resource_id is not None
        and session.phase in (SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED)
        and invocation.pending_suspension is not None
        and scope_active(context, invocation.scope)
        and observation is not None
        and observation.accepted
        and observation.status == ObservationStatus.SUCCEEDED
        and turn_source_matches(context, invocation, observation)
    )


def _checkpoint(
    state: SessionsState, context: SessionsContext, event: InvocationCheckpointAvailable
) -> AreaChange[SessionsState]:
    invocation = proven_invocation(state, event.invocation)
    if invocation is None or not _terminal(invocation):
        return AreaChange(state=state)
    if not checkpoint_matches(state, context, event):
        return AreaChange(state=state)
    if invocation.phase == SessionPhase.CHECKPOINTED:
        return AreaChange(state=state)
    phase = (
        SessionPhase.SUSPENDED
        if invocation.phase == SessionPhase.SUSPENDED
        else SessionPhase.CHECKPOINTED
    )
    signals: tuple[Signal, ...] = ()
    pending = invocation.pending_suspension
    if (
        pending is not None
        and event.retention == "wip"
        and _publication_ready(state, context, invocation)
    ):
        signals = (
            TurnSuspended(continuation=pending),
            *_yield_checkpoint_completion(context, invocation, event),
        )
        invocation = invocation.model_copy(update={"pending_suspension": None})
    state = _replace_invocation(state, invocation.model_copy(update={"phase": phase}))
    session = _session(state, event.invocation.session_id)
    if (
        session is not None
        and session.invocation == event.invocation.invocation_id
        and session.phase not in (SessionPhase.CLOSING, SessionPhase.TERMINAL)
    ):
        state = _replace_session(state, session.model_copy(update={"phase": phase}))
    return AreaChange(state=state, signals=signals)


def _run_checkpoint_request(
    state: SessionsState, context: SessionsContext, event: RunInvocationCheckpointRequested
) -> AreaChange[SessionsState]:
    invocation = proven_invocation(state, event.invocation)
    if invocation is None or invocation.scope != event.scope or not _terminal(invocation):
        return AreaChange(state=state)
    observation = invocation.observation
    claim = next((row for row in state.interrupts if row.invocation == event.invocation), None)
    if (
        observation is None
        or not turn_source_matches(context, invocation, observation)
        or event.authority != (claim.authority if claim is not None else observation.request_id)
    ):
        return AreaChange(state=state)
    session = _session(state, event.invocation.session_id)
    if session is None or session.invocation != event.invocation.invocation_id:
        return AreaChange(state=state)
    request = SnapshotAndRetainRun(
        request_id=_turn_id(event.invocation, f"checkpoint:{event.retention}"),
        scope=event.scope,
        invocation=event.invocation,
        retention=event.retention,
    )
    if request.request_id in session.pending_records or any(
        row.invocation == event.invocation and row.retention == event.retention
        for row in state.run_checkpoints
    ):
        return AreaChange(state=state)
    return AreaChange(
        state=_replace_session(
            state,
            _awaiting(
                session.model_copy(
                    update={"pending_records": (*session.pending_records, request.request_id)}
                ),
                context,
                request,
            ),
        ),
        requests=(within(request, context.run.limits.reconciliation_bound),),
    )


def _run_checkpoint_observed(
    state: SessionsState, context: SessionsContext, event: RunInvocationCheckpointObserved
) -> AreaChange[SessionsState]:
    invocation = proven_invocation(state, event.invocation)
    record = _record(context, event.checkpoint_request)
    if (
        invocation is None
        or not _terminal(invocation)
        or record is None
        or not isinstance(record.request, SnapshotAndRetainRun)
        or record.request.invocation != event.invocation
        or record.request.scope != invocation.scope
        or not isinstance(request_matches(record, record.request), Proven)
        or not isinstance(observation_for(record, event.observation), Proven)
        or not isinstance(
            fresh_observation(
                () if record.observation is None else (record.observation,),
                event.observation,
                complete=True,
            ),
            Proven,
        )
    ):
        return AreaChange(state=state)
    obs = event.observation
    if (
        event.revision is None
        or obs.revision != event.revision
        or not obs.accepted
        or not obs.terminal
        or obs.status != ObservationStatus.SUCCEEDED
    ):
        return AreaChange(state=state)
    checkpoint = RunInvocationCheckpoint(
        invocation=event.invocation,
        scope=invocation.scope,
        request_id=event.checkpoint_request,
        revision=event.revision,
        retention=record.request.retention,
    )
    previous = tuple(
        row
        for row in state.run_checkpoints
        if row.invocation == event.invocation and row.retention == checkpoint.retention
    )
    if previous:
        if previous != (checkpoint,):
            raise ContractValidationError("revision", "run checkpoint identity payload conflict")
        return AreaChange(state=state)
    state = state.model_copy(update={"run_checkpoints": (*state.run_checkpoints, checkpoint)})
    return AreaChange(
        state=state,
        signals=(
            InvocationCheckpointAvailable(
                invocation=event.invocation,
                request_id=event.checkpoint_request,
                revision=event.revision,
                retention=checkpoint.retention,
            ),
        ),
    )


def _stall_reason(
    state: SessionsState, context: SessionsContext, invocation: Invocation
) -> tuple[CheckpointFailure, CheckpointDecline | None]:
    """Why no suspension will be published, read from the records the checkpoint left."""
    ref = invocation.invocation
    owner = attempt_for(context, invocation.scope)
    if owner is None:
        retained = any(row.invocation == ref for row in state.run_checkpoints)
        failed = any(
            isinstance(row.request, SnapshotAndRetainRun)
            and row.request.invocation == ref
            and row.phase == RequestPhase.COMPLETED
            for row in context.records.records
        )
        declines = (CheckpointDecline.SNAPSHOT_FAILED,) if failed else ()
    else:
        retained = any(row.invocation == ref for row in owner.checkpoints)
        declines = tuple(row.reason for row in owner.checkpoint_declines if row.invocation == ref)
    if retained:
        return CheckpointFailure.UNPUBLISHABLE, None
    if declines:
        return CheckpointFailure.CHECKPOINT_DECLINED, declines[-1]
    return CheckpointFailure.NO_CHECKPOINT, None


def _end_yield(
    state: SessionsState, context: SessionsContext, invocation: Invocation
) -> tuple[AreaChange[SessionsState], Invocation]:
    """Make a suspending turn a failed terminal turn that its session can follow."""
    reason, decline = _stall_reason(state, context, invocation)
    ref = invocation.invocation
    ended = invocation.model_copy(
        update={"pending_suspension": None, "phase": SessionPhase.TERMINAL}
    )
    state = _replace_invocation(state, ended)
    session = _session(state, ref.session_id)
    if (
        session is not None
        and session.invocation == ref.invocation_id
        and session.phase not in (SessionPhase.CLOSING, SessionPhase.TERMINAL)
    ):
        state = _replace_session(state, session.model_copy(update={"phase": SessionPhase.IDLE}))
    signals: tuple[Signal, ...] = ()
    observation = invocation.observation
    record = _record(context, observation.request_id) if observation is not None else None
    decision_id = record.request.decision_id if record is not None else None
    receipt = (
        next((row for row in context.run.receipts if row.decision_id == decision_id), None)
        if decision_id is not None
        else None
    )
    if decision_id is not None and receipt is not None and receipt.completion is None:
        signals = (DecisionCompleted(decision_id=decision_id, status=CompletionStatus.FAILED),)
    failed = YieldPublicationFailed(invocation=ref, reason=reason, decline=decline)
    return AreaChange(state=state, signals=signals, events=(failed,)), ended


def _end_interrupt(
    state: SessionsState, context: SessionsContext, invocation: Invocation
) -> AreaChange[SessionsState]:
    """Close a draining interruption whose checkpoint cannot come: blocked, with the reason."""
    claim = next(row for row in state.interrupts if row.invocation == invocation.invocation)
    if claim.phase == "checkpointed":
        reason, decline = CheckpointFailure.REFUND_UNAPPLIED, None
    else:
        reason, decline = _stall_reason(state, context, invocation)
    blocked = claim.model_copy(update={"phase": "blocked"})
    state = state.model_copy(
        update={
            "interrupts": tuple(
                blocked if row.invocation == claim.invocation else row for row in state.interrupts
            )
        }
    )
    failed = InterruptFailed(invocation=invocation.invocation, reason=reason, decline=decline)
    return AreaChange(state=state, events=(failed,))


def end_stalled_checkpoint_waits(
    state: SessionsState, context: SessionsContext, event: CheckpointStalled
) -> tuple[AreaChange[SessionsState], Invocation | None]:
    """End the waits on a checkpoint that nothing can deliver; also the ended turn, if any.

    A suspending turn becomes a failed terminal turn: its pending suspension is dropped, its
    session can take a new turn, and its decision completes failed so dependents do not wait
    on a yield that will never come. The strategy learns why from ``YieldPublicationFailed``.
    A draining interruption is closed as blocked and the strategy learns from
    ``InterruptFailed``. A stall naming an invocation whose waits still have a producer, or
    that has none, changes nothing.
    """
    invocation = next(
        (row for row in state.invocations if row.invocation == event.invocation), None
    )
    if (
        event.invocation not in stalled_checkpoint_waits(state, context.records.records)
        or invocation is None
    ):
        # The step raises a stall only for an invocation `stalled_checkpoint_waits` names, so
        # this guards a stall submitted from outside against waits that still have a producer.
        return AreaChange(state=state), None
    interrupted = draining_without_cancel(state, event.invocation) or awaiting_refund(
        state, event.invocation
    )
    ended: Invocation | None = None
    change: AreaChange[SessionsState] = AreaChange(state=state)
    if invocation.pending_suspension is not None:
        change, ended = _end_yield(state, context, invocation)
        invocation = ended
    if interrupted:
        closed = _end_interrupt(change.state, context, invocation)
        change = AreaChange(
            state=closed.state,
            signals=change.signals,
            events=(*change.events, *closed.events),
        )
    return change, ended


def advance(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Publish only retained checkpoint-backed facts, preserving unrelated state."""
    match event:
        case InvocationCheckpointAvailable():
            return _checkpoint(state, context, event)
        case RunInvocationCheckpointRequested():
            return _run_checkpoint_request(state, context, event)
        case RunInvocationCheckpointObserved():
            return _run_checkpoint_observed(state, context, event)
        case _:
            raise ContractValidationError("event.kind", "event is not checkpoint authority")
