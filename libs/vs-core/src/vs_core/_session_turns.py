"""Pure session lease, invocation and all-role TURN accounting transitions.

The sessions wrapper owns dispatch. This leaf owns session and invocation rows,
acquisition groups and run TURN receipts; input and interruption rows are read
only. Each external action has a stable intent identity before it is returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import (
    AttemptPhase,
    InitialSessionsFailed,
    InitialSessionsReady,
    InvocationChargeRequested,
    InvocationCheckpointRequested,
    InvocationEnded,
    ReleaseDependencyObserved,
)
from .types.common import (
    AttemptId,
    AttemptRef,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    CompletionStatus,
    ContractValidationError,
    DecisionId,
    InvocationRef,
    ObservationStatus,
    ReleaseDependency,
    RequestId,
    RunStatus,
    Scope,
    SetupFailureKind,
)
from .types.evaluation import ContinuationPhase, TurnSuspended
from .types.intents import InspectRequest
from .types.kernel import AreaChange, DecisionCompleted
from .types.sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    InputAcceptanceObserved,
    InputReservationReleased,
    InputReservationRequested,
    InspectTurn,
    Invocation,
    InvocationCancellationRequested,
    InvocationChargesAuthorized,
    InvocationCheckpointAvailable,
    RegisteredTurnRequested,
    ResumeSessionTurn,
    SessionAcquisitionGroup,
    SessionDrainRequested,
    SessionObserved,
    SessionPhase,
    SessionsAcquireRequested,
    SessionsState,
    SessionView,
    TurnInputsReserved,
    TurnObserved,
    TurnRequested,
    TurnResult,
)

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import Observation, SessionId
    from .types.intents import Intent, Request
    from .types.kernel import SessionsContext, Signal
    from .types.sessions import SessionsEvent, TurnSpec


def _session(state: SessionsState, identity: SessionId) -> SessionView | None:
    return next((row for row in state.sessions if row.spec.session_id == identity), None)


def _invocation(state: SessionsState, ref: InvocationRef) -> Invocation | None:
    return next((row for row in state.invocations if row.invocation == ref), None)


def _owner(context: SessionsContext, scope: Scope) -> AttemptView | None:
    return next(
        (
            row
            for row in context.attempts.attempts
            if row.attempt_id == scope.owner and row.generation == scope.generation
        ),
        None,
    )


def _replace_session(state: SessionsState, session: SessionView) -> SessionsState:
    rows = tuple(row for row in state.sessions if row.spec.session_id != session.spec.session_id)
    # Preserve row ordering, including immutable history ordering across reloads.
    if _session(state, session.spec.session_id) is not None:
        rows = tuple(
            session if row.spec.session_id == session.spec.session_id else row
            for row in state.sessions
        )
    else:
        rows = (*rows, session)
    return state.model_copy(update={"sessions": rows})


def _replace_invocation(state: SessionsState, invocation: Invocation) -> SessionsState:
    rows = tuple(
        invocation if row.invocation == invocation.invocation else row for row in state.invocations
    )
    if _invocation(state, invocation.invocation) is None:
        rows = (*rows, invocation)
    return state.model_copy(update={"invocations": rows})


def _session_id(session: SessionView, action: str) -> RequestId:
    return RequestId(
        root=(
            f"session:{session.scope.owner.root}:{session.generation}:"
            f"{session.spec.session_id.root}:{action}"
        )
    )


def _turn_id(ref: InvocationRef, action: str) -> RequestId:
    return RequestId(
        root=f"invocation:{ref.session_id.root}:{ref.generation}:{ref.invocation_id.root}:{action}"
    )


def _intent(context: SessionsContext, identity: RequestId) -> Intent | None:
    return next((row for row in context.intents.intents if row.request_id == identity), None)


def _episode(context: SessionsContext, scope: Scope) -> DecisionId | None:
    owner = _owner(context, scope)
    return owner.admission_id if owner is not None else None


def _active(context: SessionsContext, scope: Scope) -> bool:
    if isinstance(scope.owner, AttemptId):
        owner = _owner(context, scope)
        return owner is not None and owner.phase == AttemptPhase.ACTIVE and owner.closure is None
    return (
        scope.owner == context.run.run_id
        and scope.generation == context.run.generation
        and context.run.status == RunStatus.RUNNING
    )


def _ensure(session: SessionView, context: SessionsContext, deadline: float) -> EnsureSession:
    return EnsureSession(
        request_id=_session_id(session, "ensure"),
        scope=session.scope,
        admission_id=_episode(context, session.scope),
        deadline_at=deadline,
        spec=session.spec,
        required_resource=session.resource_id,
    )


def _charged(state: SessionsState, context: SessionsContext, invocation: Invocation) -> bool:
    owner = _owner(context, invocation.scope)
    receipts = state.run_charges if owner is None else owner.charges
    kinds = {
        row.kind
        for row in receipts
        if row.invocation_id == invocation.invocation.invocation_id
        and row.charged == 1
        and row.historical_proof is None
    }
    return ChargeKind.TURN in kinds and (
        owner is None or invocation.turn.charge_class != "paid" or ChargeKind.ATTEMPT in kinds
    )


def _validate_resume(
    state: SessionsState, context: SessionsContext, ref: InvocationRef, turn: TurnSpec, scope: Scope
) -> None:
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.continuation_id == turn.continuation_id
        ),
        None,
    )
    if (
        continuation is None
        or continuation.phase != ContinuationPhase.AUTHORIZED
        or continuation.next_invocation != ref
    ):
        raise ContractValidationError("turn.continuation_id", "resume requires exact authorization")
    previous = _invocation(state, continuation.invocation)
    if (
        previous is None
        or previous.phase != SessionPhase.SUSPENDED
        or previous.scope != scope
        or previous.turn.session != turn.session
    ):
        raise ContractValidationError("turn.session", "resume must preserve yielded ownership")


def _validate_correction(
    state: SessionsState,
    context: SessionsContext,
    predecessor: Invocation | None,
    turn: TurnSpec,
    scope: Scope,
) -> None:
    if (
        predecessor is None
        or predecessor.scope != scope
        or predecessor.turn.session != turn.session
        or predecessor.observation is None
        or not predecessor.observation.terminal
    ):
        raise ContractValidationError(
            "turn.predecessor", "correction requires a terminal predecessor"
        )
    chain = predecessor
    depth = 1
    while chain.turn.charge_class == "correction":
        depth += 1
        previous = _invocation(state, chain.turn.predecessor) if chain.turn.predecessor else None
        if previous is None or depth > len(state.invocations):
            raise ContractValidationError("turn.predecessor", "invalid correction chain")
        chain = previous
    if depth > context.run.limits.max_retries:
        raise ContractValidationError("turn.predecessor", "correction retry bound exhausted")


def _validate_successor(
    state: SessionsState, context: SessionsContext, ref: InvocationRef, turn: TurnSpec, scope: Scope
) -> None:
    predecessor = _invocation(state, turn.predecessor) if turn.predecessor is not None else None
    if turn.charge_class == "resume":
        _validate_resume(state, context, ref, turn, scope)
        return
    if turn.continuation_id is not None:
        raise ContractValidationError("turn.continuation_id", "only resume may name a continuation")
    if turn.charge_class == "correction":
        _validate_correction(state, context, predecessor, turn, scope)
    elif turn.predecessor is not None:
        if (
            predecessor is None
            or predecessor.scope != scope
            or predecessor.observation is None
            or not predecessor.observation.terminal
        ):
            raise ContractValidationError(
                "turn.predecessor", "replacement requires terminal predecessor"
            )
        claim = next((row for row in state.interrupts if row.invocation == turn.predecessor), None)
        if claim is None or claim.phase != "completed":
            raise ContractValidationError(
                "turn.predecessor", "interrupted replacement requires completed proof"
            )
    if predecessor is not None and any(
        row.turn.predecessor == predecessor.invocation for row in state.invocations
    ):
        raise ContractValidationError("turn.predecessor", "predecessor already has a successor")


def _validate_session_available(session: SessionView | None, turn: TurnSpec, scope: Scope) -> None:
    if session is not None:
        if session.spec != turn.session or session.scope != scope:
            raise ContractValidationError("turn.session", "session identity ownership conflict")
        allowed = (SessionPhase.IDLE, SessionPhase.CHECKPOINTED, SessionPhase.SUSPENDED)
        if session.phase not in allowed:
            raise ContractValidationError("turn.session", "session is unavailable")
        if session.phase == SessionPhase.SUSPENDED and turn.charge_class != "resume":
            raise ContractValidationError("turn.session", "suspended session requires resume")


def _charge_run(
    state: SessionsState, context: SessionsContext, ref: InvocationRef
) -> SessionsState:
    used = sum(row.charged for row in state.run_charges if row.kind == ChargeKind.TURN)
    used += sum(
        row.charged
        for attempt in context.attempts.attempts
        for row in attempt.charges
        if row.kind == ChargeKind.TURN
    )
    if used >= context.run.limits.max_turns:
        raise ContractValidationError("run_charges", "global TURN budget exhausted")
    receipt = ChargeReceipt(
        charge_id=ChargeId(
            root=f"turn:{ref.session_id.root}:{ref.generation}:{ref.invocation_id.root}"
        ),
        kind=ChargeKind.TURN,
        invocation_id=ref.invocation_id,
        source_request=_turn_id(ref, "dispatch"),
        charged=1,
    )
    return state.model_copy(update={"run_charges": (*state.run_charges, receipt)})


def _turn_requested(
    state: SessionsState, context: SessionsContext, event: TurnRequested | RegisteredTurnRequested
) -> AreaChange[SessionsState]:
    turn = event.turn
    scope = event.scope if isinstance(event, TurnRequested) else event.request.scope
    ref = InvocationRef(
        session_id=turn.session.session_id,
        invocation_id=turn.invocation_id,
        generation=scope.generation,
    )
    existing = _invocation(state, ref)
    operation = event.request.operation_id if isinstance(event, RegisteredTurnRequested) else None
    if existing is not None:
        if (
            existing.scope != scope
            or existing.turn != turn
            or existing.registered_operation != operation
        ):
            raise ContractValidationError(
                "turn.invocation_id", "invocation identity payload conflict"
            )
        return AreaChange(state=state)
    if not _active(context, scope):
        raise ContractValidationError("scope", "turn requires current active ownership")
    if turn.deadline_at <= context.run.now_at or turn.deadline_at > context.run.deadline_at:
        raise ContractValidationError("turn.deadline_at", "turn deadline outside remaining run")
    _validate_successor(state, context, ref, turn, scope)
    session = _session(state, turn.session.session_id)
    _validate_session_available(session, turn, scope)
    invocation = Invocation(
        invocation=ref,
        scope=scope,
        turn=turn,
        registered_operation=operation,
        phase=SessionPhase.ACQUIRING,
    )
    state = _replace_invocation(state, invocation)
    owner = _owner(context, scope)
    signals: list[Signal] = []
    if owner is None:
        state = _charge_run(state, context, ref)
    else:
        signals.append(
            InvocationChargeRequested(
                attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                invocation=ref,
            )
        )
    requests: tuple[Request, ...] = ()
    if session is None:
        session = SessionView(
            spec=turn.session,
            scope=scope,
            generation=scope.generation,
            phase=SessionPhase.ACQUIRING,
            invocation=turn.invocation_id,
        )
        ensure = _ensure(session, context, turn.deadline_at)
        session = session.model_copy(update={"pending_intents": (ensure.request_id,)})
        requests = (ensure,)
    else:
        session = session.model_copy(
            update={
                "invocation": turn.invocation_id,
                "accepted": False,
                "acceptance_sequence": None,
            }
        )
        if _charged(state, context, invocation):
            signals.append(InputReservationRequested(invocation=ref))
    return AreaChange(
        state=_replace_session(state, session), signals=tuple(signals), requests=requests
    )


def _charges_authorized(
    state: SessionsState, context: SessionsContext, event: InvocationChargesAuthorized
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or invocation.phase != SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    owner = _owner(context, invocation.scope)
    if owner is None or not _active(context, invocation.scope):
        return AreaChange(state=state)
    receipts = tuple(row for row in owner.charges if row.charge_id in event.charge_ids)
    if (
        len(set(event.charge_ids)) != len(event.charge_ids)
        or len(receipts) != len(event.charge_ids)
        or any(
            row.invocation_id != event.invocation.invocation_id or row.historical_proof is not None
            for row in receipts
        )
        or not _charged(state, context, invocation)
    ):
        raise ContractValidationError("charge_ids", "authorization lacks exact recorded charges")
    session = _session(state, event.invocation.session_id)
    signals = (
        (InputReservationRequested(invocation=event.invocation),)
        if (
            session is not None
            and session.phase
            in (SessionPhase.IDLE, SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED)
        )
        else ()
    )
    return AreaChange(state=state, signals=signals)


def _dispatch_reserved(
    state: SessionsState, context: SessionsContext, event: TurnInputsReserved
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or invocation.phase != SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    session = _session(state, event.invocation.session_id)
    if (
        session is None
        or session.invocation != event.invocation.invocation_id
        or session.phase
        not in (SessionPhase.IDLE, SessionPhase.CHECKPOINTED, SessionPhase.SUSPENDED)
        or not _active(context, invocation.scope)
    ):
        return AreaChange(state=state)
    if not _charged(state, context, invocation):
        raise ContractValidationError("charge_ids", "dispatch requires recorded charge proof")
    records = tuple(
        sorted(
            (
                row
                for row in state.inputs
                if row.reserved_to == event.invocation and row.receipt is None
            ),
            key=lambda row: (row.input.sequence, row.input.input_id.root),
        )
    )
    if tuple(row.input.input_id for row in records) != event.input_ids:
        raise ContractValidationError(
            "input_ids", "manifest differs from exact reserved occurrences"
        )
    inputs = tuple(row.input for row in records)
    request: Request
    if invocation.registered_operation is not None:
        intent = next(
            (
                row
                for row in context.intents.intents
                if getattr(row.request, "operation_id", None) == invocation.registered_operation
            ),
            None,
        )
        if intent is None:
            raise ContractValidationError(
                "registered_operation", "missing canonical registered request"
            )
        request = intent.request
    elif invocation.turn.charge_class == "resume":
        if invocation.turn.continuation_id is None:
            raise ContractValidationError(
                "continuation_id", "resume requires continuation identity"
            )
        _validate_successor(state, context, event.invocation, invocation.turn, invocation.scope)
        request = ResumeSessionTurn(
            request_id=_turn_id(event.invocation, "dispatch"),
            scope=invocation.scope,
            deadline_at=invocation.turn.deadline_at,
            admission_id=_episode(context, invocation.scope),
            turn=invocation.turn,
            inputs=inputs,
            continuation_id=invocation.turn.continuation_id,
        )
    else:
        request = DispatchTurn(
            request_id=_turn_id(event.invocation, "dispatch"),
            scope=invocation.scope,
            deadline_at=invocation.turn.deadline_at,
            admission_id=_episode(context, invocation.scope),
            turn=invocation.turn,
            inputs=inputs,
        )
    invocation = invocation.model_copy(
        update={
            "phase": SessionPhase.EXECUTING,
            "input_ids": event.input_ids,
            "reserved_inputs": tuple(item.artifact for item in inputs),
        }
    )
    session = session.model_copy(
        update={
            "phase": SessionPhase.EXECUTING,
            "pending_intents": (*session.pending_intents, request.request_id),
            "continuation_id": invocation.turn.continuation_id,
        }
    )
    state = _replace_session(_replace_invocation(state, invocation), session)
    return AreaChange(state=state, requests=(request,))


def _acquire(
    state: SessionsState, context: SessionsContext, event: SessionsAcquireRequested
) -> AreaChange[SessionsState]:
    owner = _owner(context, event.scope)
    if (
        owner is None
        or event.attempt != AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
        or owner.admission_id != event.admission_id
        or owner.phase != AttemptPhase.ACQUIRING
    ):
        return AreaChange(state=state)
    ids = tuple(spec.session_id for spec in event.specs)
    if len(set(ids)) != len(ids):
        raise ContractValidationError("specs", "duplicate initial session identity")
    previous = next(
        (
            row
            for row in state.acquisition_groups
            if row.attempt == event.attempt and row.admission_id == event.admission_id
        ),
        None,
    )
    if previous is not None:
        if previous.scope != event.scope or previous.session_ids != ids:
            raise ContractValidationError("specs", "acquisition episode payload conflict")
        return AreaChange(state=state)
    group = SessionAcquisitionGroup(
        attempt=event.attempt,
        admission_id=event.admission_id,
        scope=event.scope,
        session_ids=ids,
        phase="acquiring" if ids else "ready",
    )
    requests: list[Request] = []
    for spec in event.specs:
        session = _session(state, spec.session_id)
        if session is not None:
            if session.spec != spec or session.scope != event.scope:
                raise ContractValidationError("specs", "session ownership conflict")
            if session.resource_id is None:
                raise ContractValidationError(
                    "required_resource", "reattachment lacks durable correspondence"
                )
        else:
            session = SessionView(
                spec=spec,
                scope=event.scope,
                generation=event.scope.generation,
                phase=SessionPhase.ACQUIRING,
            )
        session = session.model_copy(update={"phase": SessionPhase.ACQUIRING})
        request = _ensure(session, context, context.run.deadline_at).model_copy(
            update={"request_id": _session_id(session, f"ensure:{event.admission_id.root}")}
        )
        session = session.model_copy(update={"pending_intents": (request.request_id,)})
        state = _replace_session(state, session)
        requests.append(request)
    state = state.model_copy(update={"acquisition_groups": (*state.acquisition_groups, group)})
    signals = (
        ()
        if ids
        else (
            InitialSessionsReady(
                attempt=event.attempt, admission_id=event.admission_id, session_ids=ids
            ),
        )
    )
    return AreaChange(state=state, requests=tuple(requests), signals=signals)


def _valid_observation(
    context: SessionsContext, session: SessionView, observation: Observation
) -> Intent | None:
    intent = _intent(context, observation.request_id)
    if (
        intent is None
        or intent.request.scope != session.scope
        or observation.scope != session.scope
    ):
        return None
    if isinstance(session.scope.owner, AttemptId):
        episode = _episode(context, session.scope)
        if intent.request.admission_id != episode or observation.admission_id != episode:
            return None
    return intent


def _inspect_session(
    session: SessionView, context: SessionsContext, target: RequestId
) -> InspectRequest:
    return InspectRequest(
        request_id=RequestId(root=f"{target.root}:inspect"),
        scope=session.scope,
        deadline_at=context.run.deadline_at,
        admission_id=_episode(context, session.scope),
        target=target,
        resource_id=session.resource_id,
    )


def _close(session: SessionView, context: SessionsContext, authority: RequestId) -> CloseSession:
    return CloseSession(
        request_id=_session_id(session, f"close:{authority.root}"),
        scope=session.scope,
        deadline_at=context.run.deadline_at,
        admission_id=_episode(context, session.scope),
        session_id=session.spec.session_id,
    )


def _group_observed(
    state: SessionsState, context: SessionsContext, session: SessionView, event: SessionObserved
) -> AreaChange[SessionsState]:
    group = next(
        (
            row
            for row in state.acquisition_groups
            if row.scope == session.scope
            and row.admission_id == _episode(context, session.scope)
            and session.spec.session_id in row.session_ids
        ),
        None,
    )
    if group is None or group.phase == "ready":
        return AreaChange(state=state)
    observation = event.observation
    if group.phase == "failed":
        if session.resource_id is None or session.phase in (
            SessionPhase.CLOSING,
            SessionPhase.TERMINAL,
        ):
            return AreaChange(state=state)
        if group.failure_request is None:
            raise ContractValidationError("failure_request", "failed group lacks cleanup authority")
        close = _close(session, context, group.failure_request)
        session = session.model_copy(
            update={
                "phase": SessionPhase.CLOSING,
                "pending_intents": (*session.pending_intents, close.request_id),
            }
        )
        return AreaChange(state=_replace_session(state, session), requests=(close,))
    if (
        observation.terminal
        and not observation.accepted
        and observation.status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        group = group.model_copy(
            update={"phase": "failed", "failure_request": observation.request_id}
        )
        state = state.model_copy(
            update={
                "acquisition_groups": tuple(
                    group
                    if row.attempt == group.attempt and row.admission_id == group.admission_id
                    else row
                    for row in state.acquisition_groups
                )
            }
        )
        requests: list[Request] = []
        for identity in group.session_ids:
            member = _session(state, identity)
            if member is None or member.phase in (SessionPhase.CLOSING, SessionPhase.TERMINAL):
                continue
            if member.resource_id is not None:
                request = _close(member, context, observation.request_id)
                member = member.model_copy(
                    update={
                        "phase": SessionPhase.CLOSING,
                        "pending_intents": (*member.pending_intents, request.request_id),
                    }
                )
                state = _replace_session(state, member)
                requests.append(request)
        return AreaChange(
            state=state,
            requests=tuple(requests),
            signals=(
                InitialSessionsFailed(
                    attempt=group.attempt,
                    admission_id=group.admission_id,
                    session_id=session.spec.session_id,
                    observation=observation,
                    failure=SetupFailureKind.UNKNOWN,
                ),
            ),
        )
    if all(
        (member := _session(state, identity)) is not None
        and member.phase == SessionPhase.IDLE
        and member.resource_id is not None
        for identity in group.session_ids
    ):
        ready = group.model_copy(update={"phase": "ready"})
        state = state.model_copy(
            update={
                "acquisition_groups": tuple(
                    ready
                    if row.attempt == group.attempt and row.admission_id == group.admission_id
                    else row
                    for row in state.acquisition_groups
                )
            }
        )
        return AreaChange(
            state=state,
            signals=(
                InitialSessionsReady(
                    attempt=group.attempt,
                    admission_id=group.admission_id,
                    session_ids=group.session_ids,
                ),
            ),
        )
    return AreaChange(state=state)


def _close_observed(
    state: SessionsState, context: SessionsContext, session: SessionView, event: SessionObserved
) -> AreaChange[SessionsState]:
    observation = event.observation
    if not (
        observation.terminal
        and observation.released
        and observation.children_complete
        and observation.status == ObservationStatus.SUCCEEDED
        and session.resource_id is not None
        and observation.resource_id == session.resource_id
    ):
        return AreaChange(
            state=state, requests=(_inspect_session(session, context, observation.request_id),)
        )
    session = session.model_copy(
        update={
            "phase": SessionPhase.TERMINAL,
            "pending_intents": (),
            "acceptance_sequence": observation.sequence,
        }
    )
    state = _replace_session(state, session)
    owner = _owner(context, session.scope)
    signals = (
        ()
        if owner is None
        else (
            ReleaseDependencyObserved(
                attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                dependency=ReleaseDependency(kind="session", identity=event.session_id),
                observation=observation,
            ),
        )
    )
    return AreaChange(state=state, signals=signals)


def _ensure_observed(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    event: SessionObserved,
    request: EnsureSession,
) -> AreaChange[SessionsState]:
    observation = event.observation
    if session.phase == SessionPhase.TERMINAL:
        return AreaChange(state=state)
    ambiguous = observation.status == ObservationStatus.UNKNOWN or (
        observation.accepted
        and (
            observation.resource_id is None
            or not observation.terminal
            or observation.status != ObservationStatus.SUCCEEDED
        )
    )
    if ambiguous:
        session = session.model_copy(
            update={"phase": SessionPhase.UNKNOWN, "acceptance_sequence": observation.sequence}
        )
        return AreaChange(
            state=_replace_session(state, session),
            requests=(_inspect_session(session, context, observation.request_id),),
        )
    if observation.accepted:
        if (
            request.required_resource is not None
            and observation.resource_id != request.required_resource
        ):
            raise ContractValidationError(
                "resource_id", "reattachment substituted a different conversation"
            )
        phase = SessionPhase.IDLE
    elif observation.terminal:
        phase = SessionPhase.TERMINAL
    else:
        return AreaChange(state=state)
    session = session.model_copy(
        update={
            "phase": phase,
            "resource_id": observation.resource_id,
            "acceptance_sequence": observation.sequence,
            "pending_intents": tuple(
                identity
                for identity in session.pending_intents
                if identity != observation.request_id
            ),
        }
    )
    grouped = _group_observed(_replace_session(state, session), context, session, event)
    if grouped.signals or grouped.requests or session.invocation is None:
        return grouped
    return _reservation_after_acquisition(grouped, context, session)


def _reservation_after_acquisition(
    change: AreaChange[SessionsState], context: SessionsContext, session: SessionView
) -> AreaChange[SessionsState]:
    if session.invocation is None:
        return change
    ref = InvocationRef(
        session_id=session.spec.session_id,
        invocation_id=session.invocation,
        generation=session.generation,
    )
    invocation = _invocation(change.state, ref)
    if (
        invocation is not None
        and invocation.phase == SessionPhase.ACQUIRING
        and session.phase == SessionPhase.IDLE
        and _charged(change.state, context, invocation)
    ):
        return change.model_copy(update={"signals": (InputReservationRequested(invocation=ref),)})
    return change


def _session_observed(
    state: SessionsState, context: SessionsContext, event: SessionObserved
) -> AreaChange[SessionsState]:
    session = _session(state, event.session_id)
    if session is None or event.observation.request_id not in session.pending_intents:
        return AreaChange(state=state)
    intent = _valid_observation(context, session, event.observation)
    if intent is None:
        return AreaChange(state=state)
    observation = event.observation
    # Sequences belong to a request, not to the physical conversation globally.
    previous = intent.observation
    if previous is not None and previous.sequence > observation.sequence:
        return AreaChange(state=state)
    request = intent.request
    if isinstance(request, CloseSession) and request.session_id == event.session_id:
        return _close_observed(state, context, session, event)
    if isinstance(request, EnsureSession) and request.spec == session.spec:
        return _ensure_observed(state, context, session, event, request)
    return AreaChange(state=state)


def _turn_proof(context: SessionsContext, invocation: Invocation, observation: Observation) -> bool:
    intent = _intent(context, observation.request_id)
    if (
        intent is None
        or intent.request.scope != invocation.scope
        or observation.scope != invocation.scope
    ):
        return False
    request = intent.request
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        matches = request.turn == invocation.turn
    else:
        matches = getattr(request, "operation_id", None) == invocation.registered_operation
        matches = matches and invocation.registered_operation is not None
    if not matches:
        return False
    if isinstance(invocation.scope.owner, AttemptId):
        episode = _episode(context, invocation.scope)
        return request.admission_id == episode and observation.admission_id == episode
    return True


def _terminal_signals(
    state: SessionsState, context: SessionsContext, invocation: Invocation, event: TurnObserved
) -> tuple[Signal, ...]:
    observation = event.observation
    signals: list[Signal] = []
    owner = _owner(context, invocation.scope)
    if owner is not None:
        attempt = AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
        signals.append(
            InvocationEnded(attempt=attempt, invocation=event.invocation, observation=observation)
        )
        claim = next((row for row in state.interrupts if row.invocation == event.invocation), None)
        if claim is not None and claim.phase in ("pending", "draining"):
            signals.append(
                InvocationCheckpointRequested(
                    attempt=attempt,
                    invocation=event.invocation,
                    retention="wip",
                    authority=claim.authority,
                )
            )
    if event.suspension is not None:
        signals.append(TurnSuspended(continuation=event.suspension))
    status = {
        ObservationStatus.SUCCEEDED: CompletionStatus.SUCCEEDED,
        ObservationStatus.CANCELLED: CompletionStatus.CANCELLED,
    }.get(observation.status, CompletionStatus.FAILED)
    intent = _intent(context, observation.request_id)
    if intent is not None and intent.request.decision_id is not None:
        signals.append(DecisionCompleted(decision_id=intent.request.decision_id, status=status))
    return tuple(signals)


def _observed_phase(invocation: Invocation, event: TurnObserved) -> SessionPhase:
    observation = event.observation
    if event.output_schema is not None and event.output_schema != invocation.turn.output_schema:
        raise ContractValidationError("output_schema", "result differs from declared turn schema")
    phase = SessionPhase.EXECUTING
    if observation.status == ObservationStatus.UNKNOWN:
        phase = SessionPhase.UNKNOWN
    elif observation.terminal:
        phase = SessionPhase.SUSPENDED if event.suspension is not None else SessionPhase.TERMINAL
    if event.suspension is not None and (
        not observation.accepted
        or not observation.terminal
        or event.suspension.invocation != event.invocation
    ):
        raise ContractValidationError(
            "suspension", "yield requires exact accepted terminal invocation"
        )
    return phase


def _turn_observed(
    state: SessionsState, context: SessionsContext, event: TurnObserved
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or not _turn_proof(context, invocation, event.observation):
        return AreaChange(state=state)
    previous = invocation.observation
    if previous is not None and (
        (previous.terminal and previous.status != ObservationStatus.UNKNOWN)
        or event.observation.sequence <= previous.sequence
    ):
        return AreaChange(state=state)
    if invocation.phase == SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    observation = event.observation
    phase = _observed_phase(invocation, event)
    invocation = invocation.model_copy(
        update={
            "observation": observation,
            "phase": phase,
            "output_schema": event.output_schema,
            "output_json": event.output_json,
        }
    )
    state = _replace_invocation(state, invocation)
    session = _session(state, event.invocation.session_id)
    if session is None or session.invocation != event.invocation.invocation_id:
        return AreaChange(state=state)
    session = session.model_copy(
        update={
            "accepted": session.accepted or observation.accepted,
            "acceptance_sequence": observation.sequence,
            "phase": phase if phase != SessionPhase.TERMINAL else SessionPhase.IDLE,
        }
    )
    state = _replace_session(state, session)
    signals: list[Signal] = []
    if observation.accepted:
        signals.append(
            InputAcceptanceObserved(invocation=event.invocation, observation=observation)
        )
    elif observation.terminal and observation.status != ObservationStatus.UNKNOWN:
        signals.append(
            InputReservationReleased(invocation=event.invocation, observation=observation)
        )
    requests: tuple[Request, ...] = ()
    if phase == SessionPhase.UNKNOWN:
        requests = (
            InspectTurn(
                request_id=_turn_id(event.invocation, "inspect"),
                scope=invocation.scope,
                deadline_at=min(
                    context.run.deadline_at,
                    context.run.now_at + context.run.limits.reconciliation_bound,
                ),
                admission_id=_episode(context, invocation.scope),
                invocation=event.invocation,
            ),
        )
    if not observation.terminal or observation.status == ObservationStatus.UNKNOWN:
        return AreaChange(state=state, signals=tuple(signals), requests=requests)
    signals.extend(_terminal_signals(state, context, invocation, event))
    events = (
        TurnResult(
            invocation=event.invocation,
            observation=observation,
            output_schema=event.output_schema,
            output_json=event.output_json,
        ),
    )
    return AreaChange(state=state, signals=tuple(signals), requests=requests, events=events)


def _cancel(
    state: SessionsState, context: SessionsContext, event: InvocationCancellationRequested
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or (
        invocation.observation is not None and invocation.observation.terminal
    ):
        return AreaChange(state=state)
    session = _session(state, event.invocation.session_id)
    if session is None or session.invocation != event.invocation.invocation_id:
        return AreaChange(state=state)
    claim = next(
        (
            row
            for row in state.interrupts
            if row.invocation == event.invocation and row.authority == event.authority
        ),
        None,
    )
    authority = _intent(context, event.authority)
    if claim is None and authority is None:
        raise ContractValidationError(
            "authority", "cancellation requires recorded interruption or cleanup intent"
        )
    request = CancelTurn(
        request_id=_turn_id(event.invocation, f"cancel:{event.authority.root}"),
        scope=invocation.scope,
        deadline_at=min(
            context.run.deadline_at, context.run.now_at + context.run.limits.cancellation_bound
        ),
        admission_id=_episode(context, invocation.scope),
        invocation=event.invocation,
    )
    if request.request_id in session.pending_intents:
        return AreaChange(state=state)
    session = session.model_copy(
        update={
            "phase": SessionPhase.CLOSING,
            "pending_intents": (*session.pending_intents, request.request_id),
        }
    )
    return AreaChange(state=_replace_session(state, session), requests=(request,))


def _checkpoint(
    state: SessionsState, context: SessionsContext, event: InvocationCheckpointAvailable
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or invocation.observation is None or not invocation.observation.terminal:
        return AreaChange(state=state)
    owner = _owner(context, invocation.scope)
    if owner is None or not any(
        row.invocation == event.invocation
        and row.request_id == event.request_id
        and row.revision == event.revision
        and row.retention == event.retention
        for row in owner.checkpoints
    ):
        return AreaChange(state=state)
    if invocation.phase == SessionPhase.CHECKPOINTED:
        return AreaChange(state=state)
    state = _replace_invocation(
        state, invocation.model_copy(update={"phase": SessionPhase.CHECKPOINTED})
    )
    session = _session(state, event.invocation.session_id)
    if session is not None and session.invocation == event.invocation.invocation_id:
        state = _replace_session(
            state, session.model_copy(update={"phase": SessionPhase.CHECKPOINTED})
        )
    return AreaChange(state=state)


def _drain(
    state: SessionsState, context: SessionsContext, event: SessionDrainRequested
) -> AreaChange[SessionsState]:
    scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
    owner = _owner(context, scope)
    if owner is None or owner.closure is None or owner.closure.authority != event.authority:
        return AreaChange(state=state)
    requests: list[Request] = []
    for session in state.sessions:
        if session.scope != scope or session.phase == SessionPhase.TERMINAL:
            continue
        ref = (
            InvocationRef(
                session_id=session.spec.session_id,
                invocation_id=session.invocation,
                generation=session.generation,
            )
            if session.invocation is not None
            else None
        )
        invocation = _invocation(state, ref) if ref is not None else None
        live = invocation is not None and (
            invocation.observation is None or not invocation.observation.terminal
        )
        if live and ref is not None:
            request = CancelTurn(
                request_id=_turn_id(ref, f"cancel:{event.authority.root}"),
                scope=scope,
                deadline_at=min(
                    context.run.deadline_at,
                    context.run.now_at + context.run.limits.cancellation_bound,
                ),
                admission_id=owner.admission_id,
                invocation=ref,
            )
        else:
            request = _close(session, context, event.authority)
        if request.request_id in session.pending_intents:
            continue
        closing = session.model_copy(
            update={
                "phase": SessionPhase.CLOSING,
                "pending_intents": (*session.pending_intents, request.request_id),
            }
        )
        state = _replace_session(state, closing)
        requests.append(request)
    return AreaChange(state=state, requests=tuple(requests))


def advance(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Consume wrapper-routed events without changing input or interruption authority."""
    match event:
        case TurnRequested() | RegisteredTurnRequested():
            change = _turn_requested(state, context, event)
        case SessionsAcquireRequested():
            change = _acquire(state, context, event)
        case InvocationChargesAuthorized():
            change = _charges_authorized(state, context, event)
        case TurnInputsReserved():
            change = _dispatch_reserved(state, context, event)
        case SessionObserved():
            change = _session_observed(state, context, event)
        case TurnObserved():
            change = _turn_observed(state, context, event)
        case InvocationCancellationRequested():
            change = _cancel(state, context, event)
        case InvocationCheckpointAvailable():
            change = _checkpoint(state, context, event)
        case SessionDrainRequested():
            change = _drain(state, context, event)
        case _:
            raise ContractValidationError("event.kind", "event is owned by session inputs")
    return change
