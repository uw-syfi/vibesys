"""Pure session lease, invocation and all-role TURN accounting transitions.

Owns leases, invocations and run charges; inputs and interruptions are read-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._evaluation_history import produce_history
from .types.attempts import (
    AttemptPhase,
    AttemptSetupFailed,
    InitialSessionsFailed,
    InitialSessionsReady,
    InvocationChargeRequested,
    InvocationCheckpointRequested,
    InvocationEnded,
    ReleaseDependencyObserved,
)
from .types.common import (
    Area,
    AttemptId,
    AttemptRef,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    CompletionStatus,
    ContractValidationError,
    DecisionId,
    InvocationRef,
    KernelNotImplementedError,
    LifecycleClass,
    ObservationStatus,
    RejectionCode,
    ReleaseDependency,
    RequestId,
    RunStatus,
    Scope,
    SetupFailureKind,
)
from .types.evaluation import ContinuationPhase, TurnSuspended
from .types.evaluation_history import EvaluationHistoryAvailability
from .types.intents import ExecuteRegisteredOperation, InspectRequest, IntentPhase
from .types.kernel import AreaChange, DecisionCompleted
from .types.sessions import (
    Access,
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
    RunInvocationCheckpointRequested,
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
from .types.strategy import Accepted, Operation, Rejected, RequestTurn

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.common import Observation, SessionId
    from .types.evaluation_history import EvaluationHistoryCursor
    from .types.intents import Intent, Request
    from .types.kernel import SessionsContext, Signal
    from .types.sessions import SessionsEvent, SessionSpec, TurnSpec


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


def _identity(namespace: str, *components: str) -> str:
    """Encode supplied identity components injectively, including punctuation."""
    return namespace + ":" + ":".join(f"{len(component)}:{component}" for component in components)


def _session_id(session: SessionView, action: str) -> RequestId:
    return RequestId(
        root=_identity(
            "session",
            session.scope.owner.kind,
            session.scope.owner.root,
            str(session.scope.generation),
            str(session.generation),
            session.spec.session_id.root,
            action,
        )
    )


def _turn_id(ref: InvocationRef, action: str) -> RequestId:
    return RequestId(
        root=_identity(
            "invocation", ref.session_id.root, str(ref.generation), ref.invocation_id.root, action
        )
    )


def _intent(context: SessionsContext, identity: RequestId | None) -> Intent | None:
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
    episode = _episode(context, session.scope)
    action = "ensure" if episode is None else f"ensure:{episode.root}"
    return EnsureSession(
        request_id=_session_id(session, action),
        scope=session.scope,
        admission_id=_episode(context, session.scope),
        deadline_at=deadline,
        spec=session.spec,
        required_resource=session.resource_id,
    )


def _receipt_kinds_authorize(receipts: tuple[ChargeReceipt, ...], turn: TurnSpec) -> bool:
    required = {ChargeKind.TURN}
    if turn.charge_class == "paid":
        required.add(ChargeKind.ATTEMPT)
    return (
        len(receipts) == len(required)
        and {row.kind for row in receipts} == required
        and all(row.charged == 1 for row in receipts)
    )


def _charged(state: SessionsState, context: SessionsContext, invocation: Invocation) -> bool:
    owner = _owner(context, invocation.scope)
    receipts = state.run_charges if owner is None else owner.charges
    live = tuple(
        row
        for row in receipts
        if row.invocation_id == invocation.invocation.invocation_id and row.historical_proof is None
    )
    if owner is None:
        return len(live) == 1 and live[0].kind == ChargeKind.TURN and live[0].charged == 1
    return _receipt_kinds_authorize(live, invocation.turn)


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
        or continuation.next_invocation == continuation.invocation
        or continuation.invocation.session_id != ref.session_id
        or continuation.invocation.generation != ref.generation
        or turn.predecessor is not None
    ):
        raise ContractValidationError("turn.continuation_id", "resume requires exact authorization")
    if any(
        row.turn.continuation_id == turn.continuation_id and row.invocation != ref
        for row in state.invocations
    ):
        raise ContractValidationError(
            "turn.continuation_id", "continuation already has another resume"
        )
    if _owner(context, scope) is None and turn.session.access == Access.WRITE_CANDIDATE:
        raise ContractValidationError(
            "turn.continuation_id", "run candidate resume lacks continuation checkpoint support"
        )
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
        or not _terminal(predecessor)
        or predecessor.phase == SessionPhase.SUSPENDED
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


def _validate_interruption_fence(state: SessionsState, ref: InvocationRef, turn: TurnSpec) -> None:
    # History preserves predecessor authority after the session advances.
    previous = next(
        (
            row
            for row in reversed(state.invocations)
            if row.invocation.session_id == ref.session_id
            and row.invocation.generation == ref.generation
            and row.invocation != ref
        ),
        None,
    )
    if previous is not None and _acceptance_unresolved(previous):
        raise ContractValidationError(
            "turn.session", "session requires invocation acceptance inspection"
        )
    for claim in state.interrupts:
        if (
            claim.invocation.session_id != ref.session_id
            or claim.invocation.generation != ref.generation
            or claim.invocation == ref
        ):
            continue
        if claim.phase != "completed" or (
            previous is not None
            and previous.invocation == claim.invocation
            and turn.predecessor != claim.invocation
        ):
            raise ContractValidationError(
                "turn.predecessor", "interrupted replacement requires exact completed proof"
            )


def _validate_successor(
    state: SessionsState, context: SessionsContext, ref: InvocationRef, turn: TurnSpec, scope: Scope
) -> None:
    _validate_interruption_fence(state, ref, turn)
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
            or not _terminal(predecessor)
            or predecessor.phase == SessionPhase.SUSPENDED
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
        row.turn.predecessor == predecessor.invocation and row.invocation != ref
        for row in state.invocations
    ):
        raise ContractValidationError("turn.predecessor", "predecessor already has a successor")


def _run_reusable(session: SessionView, context: SessionsContext) -> bool:
    return (
        session.scope.owner == context.run.run_id
        and session.scope.generation == context.run.generation
        and session.spec.policy == "reuse"
        and session.spec.lifetime == "owner"
    )


def _validate_session_available(
    state: SessionsState,
    session: SessionView | None,
    context: SessionsContext,
    turn: TurnSpec,
    scope: Scope,
) -> None:
    if session is not None:
        if session.spec != turn.session or (
            session.scope != scope and not _run_reusable(session, context)
        ):
            raise ContractValidationError("turn.session", "session identity ownership conflict")
        if session.resource_id is None:
            raise ContractValidationError(
                "turn.session", "session lacks durable resource correspondence"
            )
        current = _current_invocation(state, session)
        if current is not None and (
            current.phase == SessionPhase.ACQUIRING
            or (current.phase == SessionPhase.SUSPENDED and turn.charge_class != "resume")
        ):
            raise ContractValidationError("turn.session", "session has an outstanding invocation")
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
            root=_identity("turn", ref.session_id.root, str(ref.generation), ref.invocation_id.root)
        ),
        kind=ChargeKind.TURN,
        invocation_id=ref.invocation_id,
        source_request=_turn_id(ref, "dispatch"),
        charged=1,
    )
    return state.model_copy(update={"run_charges": (*state.run_charges, receipt)})


def _registered_request(
    context: SessionsContext, request: ExecuteRegisteredOperation, turn: TurnSpec
) -> ExecuteRegisteredOperation:
    """Require the intents owner's canonical identity and registered normalization.

    Intents must persist/enrich origin correlation before sending this signal.
    Optional, uncorrelated raw requests cannot confer session dispatch authority.
    """
    canonical = _intent(context, request.request_id) if request.request_id is not None else None
    if (
        canonical is None
        or canonical.request != request
        or canonical.lifecycle != LifecycleClass.SESSION_TURN
        or request.operation.schema_ref.lifecycle != LifecycleClass.SESSION_TURN
    ):
        raise ContractValidationError(
            "registered_operation", "turn lacks exact canonical session-turn intent"
        )
    receipt = next(
        (row for row in context.run.receipts if row.decision_id == request.decision_id), None
    )
    decision = receipt.decision if receipt is not None else None
    if (
        receipt is None
        or not isinstance(receipt.feedback, Accepted)
        or not isinstance(decision, Operation)
        or decision.scope != request.scope
        or decision.deadline_at != request.deadline_at
        or decision.registered_wire is None
        or decision.registered_wire != request.operation
        or decision.normalized_turn != turn
        or decision.registered_turn != turn
    ):
        raise ContractValidationError(
            "registered_operation", "turn differs from accepted registered normalization"
        )
    if request.admission_id != _episode(context, request.scope):
        raise ContractValidationError(
            "registered_operation", "turn belongs to another admission episode"
        )
    return request


def _validate_turn_admission(context: SessionsContext, scope: Scope, turn: TurnSpec) -> None:
    if not _active(context, scope):
        raise ContractValidationError("scope", "turn requires current active ownership")
    if turn.deadline_at <= context.run.now_at or turn.deadline_at > context.run.deadline_at:
        raise ContractValidationError("turn.deadline_at", "turn deadline outside remaining run")
    if turn.charge_class == "paid" and _owner(context, scope) is None:
        raise ContractValidationError(
            "turn.charge_class", "run-scoped paid turn lacks ATTEMPT authority"
        )


def _validate_turn_origin(context: SessionsContext, scope: Scope, turn: TurnSpec) -> None:
    origins = tuple(
        receipt.decision_id
        for receipt in context.run.receipts
        if isinstance(receipt.feedback, Accepted)
        and isinstance(receipt.decision, RequestTurn)
        and receipt.decision.scope == scope
        and receipt.decision.turn == turn
    )
    if len(origins) > 1:
        raise ContractValidationError(
            "turn.invocation_id", "invocation already has an accepted decision origin"
        )


def _evaluation_prefix(
    state: SessionsState, context: SessionsContext, scope: Scope, turn: TurnSpec
) -> EvaluationHistoryCursor | None:
    if turn.charge_class in ("correction", "resume"):
        predecessor_ref = turn.predecessor
        if turn.charge_class == "resume":
            continuation = next(
                (
                    row
                    for row in context.evaluation.continuations
                    if row.continuation_id == turn.continuation_id
                ),
                None,
            )
            predecessor_ref = continuation.invocation if continuation is not None else None
        predecessor = _invocation(state, predecessor_ref) if predecessor_ref is not None else None
        return predecessor.evaluation_prefix if predecessor is not None else None
    owner = _owner(context, scope)
    if owner is None:
        return None
    history = produce_history(scope, context.evaluation, context.intents, owner, context.run)
    return (
        history.cursor if history.availability == EvaluationHistoryAvailability.COMPLETE else None
    )


def _turn_requested(
    state: SessionsState, context: SessionsContext, event: TurnRequested | RegisteredTurnRequested
) -> AreaChange[SessionsState]:
    turn = event.turn
    if isinstance(event, RegisteredTurnRequested):
        _registered_request(context, event.request, turn)
    scope = event.scope if isinstance(event, TurnRequested) else event.request.scope
    ref = InvocationRef(
        session_id=turn.session.session_id,
        invocation_id=turn.invocation_id,
        generation=scope.generation,
    )
    existing = _invocation(state, ref)
    operation = event.request.operation_id if isinstance(event, RegisteredTurnRequested) else None
    if existing is not None:
        _validate_turn_origin(context, scope, turn)
        if (
            existing.scope != scope
            or existing.turn != turn
            or existing.registered_operation != operation
        ):
            raise ContractValidationError(
                "turn.invocation_id", "invocation identity payload conflict"
            )
        return AreaChange(state=state)
    if isinstance(event, RegisteredTurnRequested):
        canonical = _intent(context, event.request.request_id)
        if canonical is not None and canonical.phase in (
            IntentPhase.COMPLETED,
            IntentPhase.BLOCKED,
        ):
            raise ContractValidationError(
                "registered_operation", "canonical turn intent is already final"
            )
    if any(row.invocation.invocation_id == ref.invocation_id for row in state.invocations):
        raise ContractValidationError(
            "turn.invocation_id", "invocation ID already belongs to another turn"
        )
    _validate_turn_admission(context, scope, turn)
    _validate_successor(state, context, ref, turn, scope)
    session = _session(state, turn.session.session_id)
    _validate_session_available(state, session, context, turn, scope)
    owner = _owner(context, scope)
    invocation = Invocation(
        invocation=ref,
        scope=scope,
        turn=turn,
        registered_operation=operation,
        phase=SessionPhase.ACQUIRING,
        evaluation_prefix=_evaluation_prefix(state, context, scope, turn),
    )
    state = _replace_invocation(state, invocation)
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
                "generation": scope.generation,
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
        or not _receipt_kinds_authorize(receipts, invocation.turn)
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


def _registered_dispatch(context: SessionsContext, invocation: Invocation) -> Request:
    intent = next(
        (
            row
            for row in context.intents.intents
            if getattr(row.request, "operation_id", None) == invocation.registered_operation
        ),
        None,
    )
    if (
        intent is None
        or not isinstance(intent.request, ExecuteRegisteredOperation)
        or intent.phase in (IntentPhase.COMPLETED, IntentPhase.BLOCKED)
    ):
        raise ContractValidationError(
            "registered_operation", "missing unfinished canonical registered request"
        )
    return _registered_request(context, intent.request, invocation.turn)


def _dispatch_reserved(
    state: SessionsState, context: SessionsContext, event: TurnInputsReserved
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or invocation.phase != SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    session = _session(state, event.invocation.session_id)
    if (
        session is None
        or session.resource_id is None
        or session.invocation != event.invocation.invocation_id
        or session.phase
        not in (SessionPhase.IDLE, SessionPhase.CHECKPOINTED, SessionPhase.SUSPENDED)
        or not _active(context, invocation.scope)
    ):
        return AreaChange(state=state)
    if invocation.turn.charge_class == "paid" and _owner(context, invocation.scope) is None:
        raise ContractValidationError(
            "turn.charge_class", "run-scoped paid turn lacks ATTEMPT authority"
        )
    if not _charged(state, context, invocation):
        raise ContractValidationError("charge_ids", "dispatch requires recorded charge proof")
    _validate_successor(state, context, event.invocation, invocation.turn, invocation.scope)
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
        if inputs:
            raise ContractValidationError(
                "input_ids", "registered operation wire has no reserved-input transport"
            )
        request = _registered_dispatch(context, invocation)
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


def _acquisition_session(
    state: SessionsState,
    context: SessionsContext,
    event: SessionsAcquireRequested,
    spec: SessionSpec,
) -> SessionView:
    session = _session(state, spec.session_id)
    if session is None:
        return SessionView(
            spec=spec,
            scope=event.scope,
            generation=event.scope.generation,
            phase=SessionPhase.ACQUIRING,
        )
    if session.spec != spec or (
        session.scope != event.scope and not _run_reusable(session, context)
    ):
        raise ContractValidationError("specs", "session ownership conflict")
    if session.resource_id is None:
        raise ContractValidationError(
            "required_resource", "reattachment lacks durable correspondence"
        )
    if session.phase not in (
        SessionPhase.IDLE,
        SessionPhase.TERMINAL,
        SessionPhase.CHECKPOINTED,
        SessionPhase.SUSPENDED,
    ):
        raise ContractValidationError(
            "specs", "session is still owned by an active acquisition or invocation"
        )
    current = _current_invocation(state, session)
    if current is not None and current.phase in (
        SessionPhase.ACQUIRING,
        SessionPhase.EXECUTING,
        SessionPhase.UNKNOWN,
    ):
        raise ContractValidationError("specs", "session has an outstanding invocation")
    return session


def _acquire(
    state: SessionsState, context: SessionsContext, event: SessionsAcquireRequested
) -> AreaChange[SessionsState]:
    owner = _owner(context, event.scope)
    if (
        owner is None
        or event.attempt != AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
        or owner.admission_id != event.admission_id
        or owner.phase != AttemptPhase.ACQUIRING
        or owner.closure is not None
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
        if (
            previous.scope != event.scope
            or previous.session_ids != ids
            or tuple(
                member.spec if (member := _session(state, identity)) is not None else None
                for identity in ids
            )
            != event.specs
        ):
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
        session = _acquisition_session(state, context, event, spec)
        session = session.model_copy(update={"phase": SessionPhase.ACQUIRING})
        request = _ensure(session, context, context.run.deadline_at).model_copy(
            update={
                "request_id": _session_id(session, f"ensure:{event.admission_id.root}"),
                "admission_id": event.admission_id,
            }
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
    if observation.admission_id != intent.request.admission_id:
        return None
    if isinstance(session.scope.owner, AttemptId):
        episode = _episode(context, session.scope)
        if intent.request.admission_id != episode or observation.admission_id != episode:
            return None
    return intent


def _inspect_session(
    session: SessionView, context: SessionsContext, target: RequestId
) -> InspectRequest:
    identity = RequestId(root=_identity("inspect-root", target.root))
    previous = _intent(context, identity)
    if previous is not None and isinstance(previous.request, InspectRequest):
        return previous.request
    original = _intent(context, target)
    return InspectRequest(
        request_id=identity,
        scope=session.scope,
        deadline_at=context.run.deadline_at,
        admission_id=original.request.admission_id
        if original is not None
        else _episode(context, session.scope),
        target=target,
        resource_id=None,
    )


def _close(session: SessionView, context: SessionsContext, authority: RequestId) -> CloseSession:
    return CloseSession(
        request_id=_session_id(session, f"close:{authority.root}"),
        scope=session.scope,
        deadline_at=context.run.deadline_at,
        admission_id=_episode(context, session.scope),
        session_id=session.spec.session_id,
    )


def _group_admitted(context: SessionsContext, group: SessionAcquisitionGroup) -> bool:
    owner = _owner(context, group.scope)
    return (
        owner is not None
        and owner.admission_id == group.admission_id
        and owner.phase == AttemptPhase.ACQUIRING
        and owner.closure is None
    )


def _group_observed(
    state: SessionsState, context: SessionsContext, session: SessionView, event: SessionObserved
) -> AreaChange[SessionsState]:
    intent = _intent(context, event.observation.request_id)
    admission = intent.request.admission_id if intent is not None else None
    group = next(
        (
            row
            for row in state.acquisition_groups
            if row.admission_id == admission and session.spec.session_id in row.session_ids
        ),
        None,
    )
    if (
        group is None
        or group.phase == "ready"
        or (group.phase == "failed" and _run_reusable(session, context))
    ):
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
            if member.resource_id is not None and not _run_reusable(member, context):
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
    if _group_admitted(context, group) and all(
        (member := _session(state, identity)) is not None
        and member.phase in (SessionPhase.IDLE, SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED)
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
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
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


def _abandon_turn_acquisition(
    state: SessionsState, context: SessionsContext, session: SessionView, observation: Observation
) -> AreaChange[SessionsState]:
    if session.invocation is None:
        return AreaChange(state=state)
    ref = InvocationRef(
        session_id=session.spec.session_id,
        invocation_id=session.invocation,
        generation=session.generation,
    )
    invocation = _invocation(state, ref)
    if invocation is None or invocation.phase != SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    invocation = invocation.model_copy(
        update={"phase": SessionPhase.TERMINAL, "observation": observation}
    )
    state = _replace_invocation(state, invocation)
    signals: list[Signal] = [InputReservationReleased(invocation=ref, observation=observation)]
    owner = _owner(context, invocation.scope)
    if owner is not None:
        signals.append(
            AttemptSetupFailed(
                attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                observation=observation,
                failure=SetupFailureKind.UNKNOWN,
            )
        )
    intent = _intent(context, observation.request_id)
    if intent is not None and intent.request.decision_id is not None:
        status = (
            CompletionStatus.CANCELLED
            if observation.status == ObservationStatus.CANCELLED
            else CompletionStatus.FAILED
        )
        signals.append(DecisionCompleted(decision_id=intent.request.decision_id, status=status))
    return AreaChange(
        state=state,
        signals=tuple(signals),
        events=(TurnResult(invocation=ref, observation=observation),),
    )


def _prepared_registered_completion(
    context: SessionsContext, invocation: Invocation
) -> tuple[Signal, ...]:
    intent = next(
        (
            row
            for row in context.intents.intents
            if isinstance(row.request, ExecuteRegisteredOperation)
            and row.request.operation_id == invocation.registered_operation
            and row.request.scope == invocation.scope
        ),
        None,
    )
    if intent is None or not isinstance(intent.request, ExecuteRegisteredOperation):
        return ()
    request = _registered_request(context, intent.request, invocation.turn)
    receipt = next(
        (row for row in context.run.receipts if row.decision_id == request.decision_id), None
    )
    if receipt is None or receipt.completion is not None:
        return ()
    return (DecisionCompleted(decision_id=receipt.decision_id, status=CompletionStatus.CANCELLED),)


def _prepared_completion(context: SessionsContext, invocation: Invocation) -> tuple[Signal, ...]:
    if invocation.registered_operation is not None:
        return _prepared_registered_completion(context, invocation)
    decision = next(
        (
            receipt.decision
            for receipt in context.run.receipts
            if isinstance(receipt.feedback, Accepted)
            and isinstance(receipt.decision, RequestTurn)
            and receipt.decision.scope == invocation.scope
            and receipt.decision.turn == invocation.turn
            and receipt.completion is None
        ),
        None,
    )
    if decision is None:
        return ()
    return (DecisionCompleted(decision_id=decision.decision_id, status=CompletionStatus.CANCELLED),)


def _after_ensure(
    state: SessionsState, context: SessionsContext, session: SessionView, event: SessionObserved
) -> AreaChange[SessionsState]:
    grouped = _group_observed(state, context, session, event)
    if grouped.signals or grouped.requests:
        return grouped
    if session.phase == SessionPhase.TERMINAL:
        return _abandon_turn_acquisition(grouped.state, context, session, event.observation)
    if session.phase == SessionPhase.UNKNOWN:
        return grouped
    owner = _owner(context, session.scope)
    if owner is not None and owner.closure is not None:
        invocation = _current_invocation(grouped.state, session)
        if invocation is not None and invocation.phase == SessionPhase.ACQUIRING:
            abandoned = invocation.model_copy(update={"phase": SessionPhase.TERMINAL})
            grouped = grouped.model_copy(
                update={"state": _replace_invocation(grouped.state, abandoned)}
            )
        request = _close(session, context, owner.closure.authority)
        closing = session.model_copy(
            update={
                "phase": SessionPhase.CLOSING,
                "pending_intents": (*session.pending_intents, request.request_id),
            }
        )
        signals = () if invocation is None else _prepared_completion(context, invocation)
        return AreaChange(
            state=_replace_session(grouped.state, closing), requests=(request,), signals=signals
        )
    if session.invocation is not None:
        return _reservation_after_acquisition(grouped, context, session)
    return grouped


def _acquired_phase(state: SessionsState, session: SessionView) -> SessionPhase:
    current = _current_invocation(state, session)
    return (
        SessionPhase.SUSPENDED
        if current is not None and current.phase == SessionPhase.SUSPENDED
        else SessionPhase.IDLE
    )


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
    ambiguous = (
        observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        or (not observation.accepted and observation.status == ObservationStatus.SUCCEEDED)
        or (
            observation.accepted
            and (
                observation.resource_id is None
                or not observation.terminal
                or observation.status != ObservationStatus.SUCCEEDED
            )
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
        phase = _acquired_phase(state, session)
    elif observation.terminal and observation.status != ObservationStatus.PENDING:
        phase = (
            SessionPhase.UNKNOWN
            if _run_reusable(session, context) and request.required_resource is not None
            else SessionPhase.TERMINAL
        )
    else:
        return AreaChange(state=state)
    session = session.model_copy(
        update={
            "phase": phase,
            "resource_id": observation.resource_id or session.resource_id,
            "acceptance_sequence": observation.sequence,
            "pending_intents": tuple(
                identity
                for identity in session.pending_intents
                if phase == SessionPhase.UNKNOWN or identity != observation.request_id
            ),
        }
    )
    change = _after_ensure(_replace_session(state, session), context, session, event)
    if phase == SessionPhase.UNKNOWN:
        change = change.model_copy(
            update={
                "requests": (
                    *change.requests,
                    _inspect_session(session, context, observation.request_id),
                )
            }
        )
    return change


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
        and _active(context, invocation.scope)
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


def _terminal(invocation: Invocation) -> bool:
    observation = invocation.observation
    return (
        observation is not None
        and observation.terminal
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def _acceptance_unresolved(invocation: Invocation) -> bool:
    observation = invocation.observation
    return (
        observation is not None
        and observation.terminal
        and observation.status == ObservationStatus.SUCCEEDED
        and not observation.accepted
    )


def _turn_proof(context: SessionsContext, invocation: Invocation, observation: Observation) -> bool:
    intent = _intent(context, observation.request_id)
    if (
        intent is None
        or intent.request.scope != invocation.scope
        or observation.scope != invocation.scope
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
        if event.suspension is not None or (
            claim is not None and claim.phase in ("pending", "draining")
        ):
            signals.append(
                InvocationCheckpointRequested(
                    attempt=attempt,
                    invocation=event.invocation,
                    retention="wip",
                    authority=claim.authority if claim is not None else observation.request_id,
                )
            )
    elif event.suspension is not None:
        signals.append(
            RunInvocationCheckpointRequested(
                invocation=event.invocation,
                scope=invocation.scope,
                retention="wip",
                authority=observation.request_id,
            )
        )
    status = {
        ObservationStatus.SUCCEEDED: CompletionStatus.SUCCEEDED,
        ObservationStatus.CANCELLED: CompletionStatus.CANCELLED,
    }.get(observation.status, CompletionStatus.FAILED)
    intent = _intent(context, observation.request_id)
    if (
        intent is not None
        and intent.request.decision_id is not None
        and not (event.suspension is not None and status == CompletionStatus.SUCCEEDED)
    ):
        # Checkpoint failure preserves the yielded decision dependency fence.
        signals.append(DecisionCompleted(decision_id=intent.request.decision_id, status=status))
    return tuple(signals)


def _observed_phase(invocation: Invocation, event: TurnObserved) -> SessionPhase:
    observation = event.observation
    if event.output_schema is not None and event.output_schema != invocation.turn.output_schema:
        raise ContractValidationError("output_schema", "result differs from declared turn schema")
    phase = SessionPhase.EXECUTING
    if observation.status == ObservationStatus.UNKNOWN:
        phase = SessionPhase.UNKNOWN
    elif observation.terminal and observation.status != ObservationStatus.PENDING:
        phase = SessionPhase.SUSPENDED if event.suspension is not None else SessionPhase.TERMINAL
    if event.suspension is not None and (
        not observation.accepted
        or not observation.terminal
        or observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        or event.suspension.invocation != event.invocation
    ):
        raise ContractValidationError(
            "suspension", "yield requires exact accepted terminal invocation"
        )
    return phase


def _inspect_turn(context: SessionsContext, invocation: Invocation) -> InspectTurn:
    identity = _turn_id(invocation.invocation, "inspect")
    previous = _intent(context, identity)
    if previous is not None and isinstance(previous.request, InspectTurn):
        return previous.request
    return InspectTurn(
        request_id=identity,
        scope=invocation.scope,
        deadline_at=min(
            context.run.deadline_at, context.run.now_at + context.run.limits.reconciliation_bound
        ),
        admission_id=_episode(context, invocation.scope),
        invocation=invocation.invocation,
    )


def _after_turn_cleanup(
    state: SessionsState, context: SessionsContext, invocation: Invocation, session: SessionView
) -> AreaChange[SessionsState]:
    owner = _owner(context, invocation.scope)
    if owner is None or owner.closure is None:
        return AreaChange(state=state)
    if session.phase == SessionPhase.TERMINAL and not _run_reusable(session, context):
        return AreaChange(state=state)
    if _run_reusable(session, context):
        observation = invocation.observation
        if observation is None or not (observation.released and observation.children_complete):
            return AreaChange(state=state, requests=(_inspect_turn(context, invocation),))
        dependency = ReleaseDependency(kind="request", identity=observation.request_id)
        signals = (
            ()
            if dependency not in owner.release_dependencies
            else (
                ReleaseDependencyObserved(
                    attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                    dependency=dependency,
                    observation=observation,
                ),
            )
        )
        return AreaChange(state=state, signals=signals)
    request = _close(session, context, owner.closure.authority)
    if request.request_id in session.pending_intents:
        return AreaChange(state=state)
    closing = session.model_copy(
        update={
            "phase": SessionPhase.CLOSING,
            "pending_intents": (*session.pending_intents, request.request_id),
        }
    )
    return AreaChange(state=_replace_session(state, closing), requests=(request,))


def _late_turn_observed(
    state: SessionsState,
    context: SessionsContext,
    invocation: Invocation,
    event: TurnObserved,
    *,
    inspected: bool = False,
) -> AreaChange[SessionsState]:
    previous = invocation.observation
    observation = event.observation
    if (
        previous is None
        or (not inspected and observation.sequence <= previous.sequence)
        or observation.status != previous.status
        or not observation.terminal
        or observation.request_id != previous.request_id
        or (previous.resource_id is not None and observation.resource_id != previous.resource_id)
    ):
        return AreaChange(state=state)
    if (
        observation.accepted
        and not previous.accepted
        and previous.status
        in (ObservationStatus.REJECTED, ObservationStatus.FAILED, ObservationStatus.CANCELLED)
    ):
        raise ContractValidationError(
            "observation.accepted", "acceptance contradicts positive nonacceptance"
        )
    accepted = previous.accepted or observation.accepted
    # The immutable terminal output stays final. Correlated later facts strengthen
    # lease/delivery proof; absent flags cannot revoke earlier positive evidence.
    known_children = set(previous.children)
    incoming_children = set(observation.children)
    complete = (observation.children_complete and known_children <= incoming_children) or (
        previous.children_complete and incoming_children <= known_children
    )
    proof = observation.model_copy(
        update={
            "accepted": accepted,
            "released": previous.released or observation.released,
            "children_complete": complete,
            "resource_id": observation.resource_id or previous.resource_id,
            "children": tuple(dict.fromkeys((*previous.children, *observation.children))),
        }
    )
    invocation = invocation.model_copy(update={"observation": proof})
    state = _replace_invocation(state, invocation)
    signals: tuple[Signal, ...] = ()
    if observation.accepted and not previous.accepted:
        signals = (InputAcceptanceObserved(invocation=event.invocation, observation=observation),)
    session = _session(state, invocation.invocation.session_id)
    if session is None or session.invocation != invocation.invocation.invocation_id:
        return AreaChange(state=state, signals=signals)
    session = session.model_copy(
        update={
            "accepted": session.accepted or accepted,
            "phase": _physical_turn_phase(
                session,
                context,
                SessionPhase.UNKNOWN if _acceptance_unresolved(invocation) else invocation.phase,
            ),
        }
    )
    state = _replace_session(state, session)
    cleanup = _after_turn_cleanup(state, context, invocation, session)
    requests = (_inspect_turn(context, invocation),) if _acceptance_unresolved(invocation) else ()
    return cleanup.model_copy(
        update={
            "signals": (*signals, *cleanup.signals),
            "requests": tuple(dict.fromkeys((*requests, *cleanup.requests))),
        }
    )


def _physical_turn_phase(
    session: SessionView, context: SessionsContext, phase: SessionPhase
) -> SessionPhase:
    physical_phase = phase if phase != SessionPhase.TERMINAL else SessionPhase.IDLE
    if session.phase == SessionPhase.TERMINAL or (
        session.phase == SessionPhase.CLOSING
        and any(
            row.request_id in session.pending_intents and isinstance(row.request, CloseSession)
            for row in context.intents.intents
        )
    ):
        physical_phase = session.phase
    return physical_phase


def _correlated_turn_observation(
    context: SessionsContext, invocation: Invocation, event: TurnObserved
) -> tuple[TurnObserved, bool]:
    previous = invocation.observation
    inspection = _intent(context, event.observation.request_id)
    inspected = inspection is not None and isinstance(inspection.request, InspectTurn)
    if inspected and previous is not None:
        # Inspection strengthens the original dispatch delivery facts.
        event = event.model_copy(
            update={
                "observation": event.observation.model_copy(
                    update={
                        "request_id": previous.request_id,
                        "sequence": max(previous.sequence, event.observation.sequence),
                    }
                )
            }
        )
    return event, inspected


def _turn_observed(
    state: SessionsState, context: SessionsContext, event: TurnObserved
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or not _turn_proof(context, invocation, event.observation):
        return AreaChange(state=state)
    previous = invocation.observation
    event, inspected = _correlated_turn_observation(context, invocation, event)
    if _terminal(invocation):
        return _late_turn_observed(state, context, invocation, event, inspected=inspected)
    if (
        not inspected and previous is not None and event.observation.sequence <= previous.sequence
    ) or invocation.phase == SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    observation = event.observation
    if event.suspension is not None and observation.status != ObservationStatus.SUCCEEDED:
        event = event.model_copy(update={"suspension": None})
    phase = _observed_phase(invocation, event)
    invocation = invocation.model_copy(
        update={
            "observation": observation,
            "phase": phase,
            "output_schema": event.output_schema,
            "output_json": event.output_json,
            "pending_suspension": event.suspension,
        }
    )
    state = _replace_invocation(state, invocation)
    session = _session(state, event.invocation.session_id)
    if session is None or session.invocation != event.invocation.invocation_id:
        return AreaChange(state=state)
    session = session.model_copy(
        update={
            "accepted": session.accepted
            or (observation.accepted and observation.status != ObservationStatus.UNKNOWN),
            "acceptance_sequence": observation.sequence,
            "phase": _physical_turn_phase(
                session,
                context,
                SessionPhase.UNKNOWN if _acceptance_unresolved(invocation) else phase,
            ),
        }
    )
    state = _replace_session(state, session)
    signals: list[Signal] = []
    if observation.accepted and observation.status != ObservationStatus.UNKNOWN:
        signals.append(
            InputAcceptanceObserved(invocation=event.invocation, observation=observation)
        )
    elif _terminal(invocation) and observation.status in (
        ObservationStatus.REJECTED,
        ObservationStatus.FAILED,
        ObservationStatus.CANCELLED,
    ):
        signals.append(
            InputReservationReleased(invocation=event.invocation, observation=observation)
        )
    requests: tuple[Request, ...] = ()
    if phase == SessionPhase.UNKNOWN or _acceptance_unresolved(invocation):
        requests = (_inspect_turn(context, invocation),)
    if not _terminal(invocation):
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
    cleanup = _after_turn_cleanup(state, context, invocation, session)
    return AreaChange(
        state=cleanup.state,
        signals=(*signals, *cleanup.signals),
        requests=tuple(dict.fromkeys((*requests, *cleanup.requests))),
        events=events,
    )


def _cancel(
    state: SessionsState, context: SessionsContext, event: InvocationCancellationRequested
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or _terminal(invocation):
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
    owner = _owner(context, invocation.scope)
    cleanup = (
        owner is not None
        and owner.closure is not None
        and owner.closure.authority == event.authority
        and owner.closure.admission_id == owner.admission_id
    )
    if claim is None and not cleanup:
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
        or not _turn_proof(context, invocation, observation)
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


def _checkpoint(
    state: SessionsState, context: SessionsContext, event: InvocationCheckpointAvailable
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or not _terminal(invocation):
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
    phase = (
        SessionPhase.SUSPENDED
        if invocation.phase == SessionPhase.SUSPENDED
        else SessionPhase.CHECKPOINTED
    )
    signals = _yield_checkpoint_completion(context, invocation, event)
    pending = invocation.pending_suspension
    if pending is not None and event.retention == "wip" and _active(context, invocation.scope):
        signals = (TurnSuspended(continuation=pending), *signals)
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


def _current_invocation(state: SessionsState, session: SessionView) -> Invocation | None:
    return next(
        (
            row
            for row in state.invocations
            if row.invocation.session_id == session.spec.session_id
            and row.invocation.invocation_id == session.invocation
            and row.invocation.generation == session.generation
        ),
        None,
    )


def _drain_acquiring(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    invocation: Invocation | None,
) -> AreaChange[SessionsState]:
    ensures = tuple(
        row
        for row in context.intents.intents
        if row.request_id in session.pending_intents and isinstance(row.request, EnsureSession)
    )
    if ensures:
        return AreaChange(
            state=state,
            requests=tuple(_inspect_session(session, context, row.request_id) for row in ensures),
        )
    if invocation is None:
        return AreaChange(state=state)
    abandoned = invocation.model_copy(update={"phase": SessionPhase.TERMINAL})
    state = _replace_invocation(state, abandoned)
    if _run_reusable(session, context):
        detached = session.model_copy(
            update={
                "phase": SessionPhase.IDLE,
                "invocation": None,
                "accepted": False,
                "continuation_id": None,
            }
        )
        cleanup = AreaChange(state=_replace_session(state, detached))
    else:
        cleanup = _after_turn_cleanup(state, context, abandoned, session)
    return cleanup.model_copy(
        update={"signals": (*cleanup.signals, *_prepared_completion(context, invocation))}
    )


def _drain_lease(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    event: SessionDrainRequested,
) -> AreaChange[SessionsState]:
    if session.resource_id is None:
        ensures = tuple(
            row
            for row in context.intents.intents
            if row.request_id in session.pending_intents and isinstance(row.request, EnsureSession)
        )
        return AreaChange(
            state=state,
            requests=tuple(_inspect_session(session, context, row.request_id) for row in ensures),
        )
    request = _close(session, context, event.authority)
    if request.request_id in session.pending_intents:
        return AreaChange(state=state)
    closing = session.model_copy(
        update={
            "phase": SessionPhase.CLOSING,
            "pending_intents": (*session.pending_intents, request.request_id),
        }
    )
    return AreaChange(state=_replace_session(state, closing), requests=(request,))


def _drain_session(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    event: SessionDrainRequested,
) -> AreaChange[SessionsState]:
    invocation = _current_invocation(state, session)
    scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
    if (
        session.scope != scope and (invocation is None or invocation.scope != scope)
    ) or session.phase == SessionPhase.TERMINAL:
        return AreaChange(state=state)
    if invocation is not None and invocation.phase == SessionPhase.ACQUIRING:
        return _drain_acquiring(state, context, session, invocation)
    if (
        invocation is not None
        and invocation.phase == SessionPhase.TERMINAL
        and invocation.observation is None
    ):
        return _drain_lease(state, context, session, event)
    if invocation is not None and not _terminal(invocation):
        return _cancel(
            state,
            context,
            InvocationCancellationRequested(
                invocation=invocation.invocation, authority=event.authority
            ),
        )
    if _run_reusable(session, context) and invocation is not None:
        return _after_turn_cleanup(state, context, invocation, session)
    return _drain_lease(state, context, session, event)


def _drain(
    state: SessionsState, context: SessionsContext, event: SessionDrainRequested
) -> AreaChange[SessionsState]:
    scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
    owner = _owner(context, scope)
    if owner is None or owner.closure is None or owner.closure.authority != event.authority:
        return AreaChange(state=state)
    requests: list[Request] = []
    signals: list[Signal] = []
    groups = tuple(
        group.model_copy(update={"phase": "failed", "failure_request": event.authority})
        if group.attempt == event.attempt
        and group.admission_id == owner.admission_id
        and group.phase == "acquiring"
        else group
        for group in state.acquisition_groups
    )
    state = state.model_copy(update={"acquisition_groups": groups})
    for session in state.sessions:
        change = _drain_session(state, context, session, event)
        state = change.state
        requests.extend(change.requests)
        signals.extend(change.signals)
    return AreaChange(state=state, requests=tuple(requests), signals=tuple(signals))


def _turn_failure(
    state: SessionsState,
    context: SessionsContext,
    event: TurnRequested | RegisteredTurnRequested,
    error: ContractValidationError,
) -> AreaChange[SessionsState]:
    if isinstance(event, RegisteredTurnRequested):
        raise error
    scope = event.scope if isinstance(event, TurnRequested) else event.request.scope
    decision = next(
        (
            receipt.decision
            for receipt in reversed(context.run.receipts)
            if receipt.completion is None
            and isinstance(receipt.feedback, Accepted)
            and (
                (
                    isinstance(receipt.decision, RequestTurn)
                    and receipt.decision.scope == scope
                    and receipt.decision.turn == event.turn
                )
                or (
                    isinstance(receipt.decision, Operation)
                    and receipt.decision.scope == scope
                    and receipt.decision.normalized_turn == event.turn
                )
            )
        ),
        None,
    )
    if decision is None:
        raise error
    path, _, detail = str(error).partition(": ")
    codes = {
        "run_charges": RejectionCode.BUDGET,
        "turn.predecessor": RejectionCode.DEPENDENCY,
        "turn.continuation_id": RejectionCode.DEPENDENCY,
        "turn.invocation_id": RejectionCode.IDENTITY_CONFLICT,
        "turn.session": RejectionCode.OWNERSHIP,
        "scope": RejectionCode.CLOSED_SCOPE,
        "turn.deadline_at": RejectionCode.BUDGET,
    }
    feedback = Rejected(
        decision_id=decision.decision_id,
        code=RejectionCode.BUDGET
        if "bound exhausted" in detail
        else codes.get(path, RejectionCode.OWNERSHIP),
        path=tuple(path.split(".")),
        detail=detail,
    )
    return AreaChange(state=state, events=(feedback,))


def _admit_turn(
    state: SessionsState, context: SessionsContext, event: TurnRequested | RegisteredTurnRequested
) -> AreaChange[SessionsState]:
    try:
        return _turn_requested(state, context, event)
    except ContractValidationError as error:
        return _turn_failure(state, context, event, error)


def advance(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Consume wrapper-routed events without changing input or interruption authority."""
    match event:
        case TurnRequested() | RegisteredTurnRequested():
            change = _admit_turn(state, context, event)
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


def advance_run_authority(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Declare Sessions A's new run drain/checkpoint route pending leaf adoption."""
    del state, context
    raise KernelNotImplementedError(Area.SESSIONS, event.kind, subarea="_session_turns")
