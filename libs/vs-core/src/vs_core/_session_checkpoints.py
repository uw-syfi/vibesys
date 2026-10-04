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
from ._session_scope import attempt_for, proven_invocation, scope_active
from .types.attempts import SnapshotAndRetain
from .types.common import (
    AttemptId,
    AttemptRef,
    CompletionStatus,
    ContractValidationError,
    LifecycleClass,
    ObservationStatus,
    RequestId,
)
from .types.evaluation import TurnSuspended
from .types.intents import ExecuteRegisteredOperation
from .types.kernel import AreaChange, DecisionCompleted
from .types.sessions import (
    DispatchTurn,
    InspectTurn,
    InvocationCheckpointAvailable,
    ResumeSessionTurn,
    RunInvocationCheckpoint,
    RunInvocationCheckpointObserved,
    RunInvocationCheckpointRequested,
    SessionPhase,
    SnapshotAndRetainRun,
)

if TYPE_CHECKING:
    from .types.common import InvocationRef, Observation, SessionId
    from .types.intents import Intent
    from .types.kernel import SessionsContext, Signal
    from .types.sessions import Invocation, SessionsEvent, SessionsState, SessionView


def _session(state: SessionsState, identity: SessionId) -> SessionView | None:
    return next((row for row in state.sessions if row.spec.session_id == identity), None)


def _replace_session(state: SessionsState, session: SessionView) -> SessionsState:
    return state.model_copy(
        update={
            "sessions": tuple(
                session if row.spec.session_id == session.spec.session_id else row
                for row in state.sessions
            )
        }
    )


def _replace_invocation(state: SessionsState, invocation: Invocation) -> SessionsState:
    return state.model_copy(
        update={
            "invocations": tuple(
                invocation if row.invocation == invocation.invocation else row
                for row in state.invocations
            )
        }
    )


def _intent(context: SessionsContext, identity: RequestId | None) -> Intent | None:
    return next((row for row in context.intents.intents if row.request_id == identity), None)


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
    intent = _intent(context, observation.request_id)
    if (
        intent is None
        or intent.request.scope != invocation.scope
        or not isinstance(
            invocation_for((invocation,), invocation.invocation, invocation.scope), Proven
        )
        or not isinstance(request_matches(intent, intent.request), Proven)
        or not isinstance(observation_for(intent, observation), Proven)
    ):
        return False
    request = intent.request
    if isinstance(request, InspectTurn):
        matches = request.invocation == invocation.invocation
    elif isinstance(request, DispatchTurn | ResumeSessionTurn):
        matches = request.turn == invocation.turn
    else:
        matches = (
            isinstance(request, ExecuteRegisteredOperation)
            and intent.lifecycle == LifecycleClass.SESSION_TURN
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
    intent = _intent(context, observation.request_id)
    if intent is None or intent.request.decision_id is None:
        return ()
    decision_id = intent.request.decision_id
    receipt = next((row for row in context.run.receipts if row.decision_id == decision_id), None)
    if receipt is None or receipt.completion is not None:
        return ()
    return (DecisionCompleted(decision_id=decision_id, status=CompletionStatus.SUCCEEDED),)


def checkpoint_matches(
    state: SessionsState, context: SessionsContext, event: InvocationCheckpointAvailable
) -> bool:
    """Prove one retained receipt with canonical snapshot payload and terminal source."""
    invocation = proven_invocation(state, event.invocation)
    intent = _intent(context, event.request_id)
    if (
        invocation is None
        or not _terminal(invocation)
        or invocation.observation is None
        or not turn_source_matches(context, invocation, invocation.observation)
        or intent is None
        or not isinstance(request_matches(intent, intent.request), Proven)
        or intent.request.scope != invocation.scope
    ):
        return False
    owner = attempt_for(context, invocation.scope)
    request = intent.request
    if owner is None:
        if not isinstance(request, SnapshotAndRetainRun) or request.invocation != event.invocation:
            return False
        proofs = state.run_checkpoints
    else:
        if (
            not isinstance(request, SnapshotAndRetain)
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
        deadline_at=min(
            context.run.deadline_at, context.run.now_at + context.run.limits.reconciliation_bound
        ),
    )
    if request.request_id in session.pending_intents or any(
        row.invocation == event.invocation and row.retention == event.retention
        for row in state.run_checkpoints
    ):
        return AreaChange(state=state)
    return AreaChange(
        state=_replace_session(
            state,
            session.model_copy(
                update={
                    "pending_intents": (*session.pending_intents, request.request_id),
                }
            ),
        ),
        requests=(request,),
    )


def _run_checkpoint_observed(
    state: SessionsState, context: SessionsContext, event: RunInvocationCheckpointObserved
) -> AreaChange[SessionsState]:
    invocation = proven_invocation(state, event.invocation)
    intent = _intent(context, event.checkpoint_request)
    if (
        invocation is None
        or not _terminal(invocation)
        or intent is None
        or not isinstance(intent.request, SnapshotAndRetainRun)
        or intent.request.invocation != event.invocation
        or intent.request.scope != invocation.scope
        or not isinstance(request_matches(intent, intent.request), Proven)
        or not isinstance(observation_for(intent, event.observation), Proven)
        or not isinstance(
            fresh_observation(
                () if intent.observation is None else (intent.observation,),
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
        retention=intent.request.retention,
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
