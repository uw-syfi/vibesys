"""Admitting a turn: validating its request, charging it, and reserving it for dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._deadlines import from_dispatch
from ._evaluation_history import produce_history
from ._proofs import (
    Proven,
    accepted_receipt_for,
    current_admission,
    invocation_for,
    request_matches,
)
from ._session_inputs import standing_guidance
from ._session_rows import awaiting as _awaiting
from ._session_rows import invocation_row as _invocation
from ._session_rows import replace_invocation as _replace_invocation
from ._session_rows import replace_session as _replace_session
from ._session_rows import session_row as _session
from ._session_scope import (
    attempt_for,
    encode_identity,
    scope_active,
    turn_request_id,
)
from ._session_state import (
    _current_invocation,
    _ensure,
    _episode,
    _record,
    _run_reusable,
)
from ._turn_holds import lost_to_provider
from ._turn_outcomes import (
    acceptance_unresolved,
    is_terminal,
)
from .types.attempts import (
    InvocationChargeRequested,
)
from .types.common import (
    MAX_REASK_CHAIN,
    AttemptRef,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    ContinuationId,
    ContractValidationError,
    InvocationRef,
    LifecycleClass,
    RejectionCode,
    ReservedInputOccurrence,
    Scope,
)
from .types.evaluation import ContinuationPhase, ResumeAuthorizationReceipt
from .types.evaluation_history import EvaluationHistoryAvailability
from .types.kernel import AreaChange
from .types.records import (
    ExecuteRegisteredOperation,
    Outbound,
    RequestPhase,
)
from .types.sessions import (
    Access,
    DispatchTurn,
    InputReservationRequested,
    Invocation,
    InvocationChargesAuthorized,
    RegisteredTurnRequested,
    ResumeSessionTurn,
    SessionPhase,
    SessionsState,
    SessionView,
    TurnInputsReserved,
    TurnRequested,
)
from .types.strategy import Accepted, Operation, Rejected, RequestTurn
from .types.until import FromDispatch, Until
from .types.waits import WaitKind, WaitOn, WaitSubject

if TYPE_CHECKING:
    from .types.evaluation_history import EvaluationHistoryCursor
    from .types.kernel import SessionsContext, Signal
    from .types.records import RecordsState, Request
    from .types.sessions import TurnSpec


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
    owner = attempt_for(context, invocation.scope)
    receipts = state.run_charges if owner is None else owner.charges
    live = tuple(
        row
        for row in receipts
        if row.invocation_id == invocation.invocation.invocation_id and row.historical_proof is None
    )
    if owner is None:
        return len(live) == 1 and live[0].kind == ChargeKind.TURN and live[0].charged == 1
    return _receipt_kinds_authorize(live, invocation.turn)


def _publication(
    context: SessionsContext, continuation_id: ContinuationId
) -> ResumeAuthorizationReceipt | None:
    """The stored publication proof of the authorized continuation."""
    return next(
        (
            row.authorization_receipt
            for row in context.evaluation.continuations
            if row.continuation_id == continuation_id
        ),
        None,
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
    if attempt_for(context, scope) is None and turn.session.access == Access.WRITE_CANDIDATE:
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
    records: RecordsState,
    predecessor: Invocation | None,
    turn: TurnSpec,
    scope: Scope,
) -> None:
    if (
        predecessor is None
        or predecessor.scope != scope
        or predecessor.turn.session != turn.session
        or predecessor.observation is None
        or not is_terminal(predecessor)
        or predecessor.phase == SessionPhase.SUSPENDED
    ):
        raise ContractValidationError(
            "turn.predecessor", "correction requires a terminal predecessor"
        )
    # A re-ask after a turn lost to its provider is no link: the gate bounds an outage.
    chain = predecessor
    links = 1
    depth = 0 if lost_to_provider(records, predecessor) else 1
    while chain.turn.charge_class == "correction":
        links += 1
        previous = _invocation(state, chain.turn.predecessor) if chain.turn.predecessor else None
        if previous is None or links > len(state.invocations):
            raise ContractValidationError("turn.predecessor", "invalid correction chain")
        depth += 0 if lost_to_provider(records, previous) else 1
        chain = previous
    if depth > MAX_REASK_CHAIN:
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
    if previous is not None and acceptance_unresolved(previous):
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
        _validate_correction(state, context.records, predecessor, turn, scope)
    elif turn.predecessor is not None:
        if (
            predecessor is None
            or predecessor.scope != scope
            or predecessor.observation is None
            or not is_terminal(predecessor)
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
            root=encode_identity(
                "turn", ref.session_id.root, str(ref.generation), ref.invocation_id.root
            )
        ),
        kind=ChargeKind.TURN,
        invocation_id=ref.invocation_id,
        source_request=turn_request_id(ref, "dispatch"),
        charged=1,
    )
    return state.model_copy(update={"run_charges": (*state.run_charges, receipt)})


def _registered_request(
    context: SessionsContext, request: ExecuteRegisteredOperation, turn: TurnSpec
) -> ExecuteRegisteredOperation:
    """Require canonical wire, accepted origin and exact registered turn normalization."""
    canonical = _record(context, request.request_id)
    proof = accepted_receipt_for(context.run.receipts, request.decision_id, None)
    if not isinstance(request_matches(canonical, request), Proven) or not isinstance(proof, Proven):
        raise ContractValidationError(
            "registered_operation", "turn lacks exact canonical session-turn record"
        )
    decision = proof.value.decision
    if (
        not isinstance(decision, Operation)
        or decision.scope != request.scope
        or decision.registered_wire != request.operation
        or decision.normalized_turn != turn
        or decision.registered_turn != turn
        or request.operation.schema_ref.lifecycle != LifecycleClass.SESSION_TURN
        or request.admission_id != _episode(context, request.scope)
    ):
        raise ContractValidationError(
            "registered_operation", "turn differs from accepted registered normalization"
        )
    return request


def _validate_turn_admission(context: SessionsContext, scope: Scope, turn: TurnSpec) -> None:
    if not scope_active(context, scope):
        raise ContractValidationError("scope", "turn requires current active ownership")
    if not context.run.deadline_at > context.run.now_at:
        raise ContractValidationError("until", "run has no time left for the turn")
    if turn.charge_class == "paid" and attempt_for(context, scope) is None:
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
    if any(
        not isinstance(accepted_receipt_for(context.run.receipts, identity, None), Proven)
        for identity in origins
    ):
        raise ContractValidationError("turn.invocation_id", "invalid accepted decision origin")
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
        proof = (
            invocation_for(state.invocations, predecessor_ref, scope)
            if predecessor_ref is not None
            else None
        )
        return proof.value.evaluation_prefix if isinstance(proof, Proven) else None
    owner = attempt_for(context, scope)
    if owner is None or not isinstance(current_admission(owner, scope, owner.admission_id), Proven):
        return None
    history = produce_history(scope, context.evaluation, context.records, owner, context.run)
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
        canonical = _record(context, event.request.request_id)
        if canonical is not None and canonical.phase in (
            RequestPhase.COMPLETED,
            RequestPhase.BLOCKED,
        ):
            raise ContractValidationError(
                "registered_operation", "canonical turn record is already final"
            )
    if any(row.invocation.invocation_id == ref.invocation_id for row in state.invocations):
        raise ContractValidationError(
            "turn.invocation_id", "invocation ID already belongs to another turn"
        )
    _validate_turn_admission(context, scope, turn)
    _validate_successor(state, context, ref, turn, scope)
    session = _session(state, turn.session.session_id)
    _validate_session_available(state, session, context, turn, scope)
    owner = attempt_for(context, scope)
    invocation = Invocation(
        invocation=ref,
        scope=scope,
        turn=turn,
        registered_operation=operation,
        phase=SessionPhase.ACQUIRING,
        evaluation_prefix=_evaluation_prefix(state, context, scope, turn),
        until=event.until,
        limit=from_dispatch(context.run, event.until).limit,
        # The session's acquisition, or its reservation and dispatch once it holds the
        # lease, carries the turn on; an idle session already holding it does so this step.
        wait=WaitOn(
            subjects=(WaitSubject(kind=WaitKind.SESSION, identity=turn.session.session_id.root),)
        ),
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
    requests: tuple[Outbound, ...] = ()
    if session is None:
        session = SessionView(
            spec=turn.session,
            scope=scope,
            generation=scope.generation,
            phase=SessionPhase.ACQUIRING,
            invocation=turn.invocation_id,
        )
        ensure = _ensure(session, context)
        session = _awaiting(
            session.model_copy(update={"pending_records": (ensure.request_id,)}), context, ensure
        )
        requests = (Outbound(request=ensure, until=_from_dispatch(invocation)),)
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


def _from_dispatch(invocation: Invocation) -> FromDispatch | Until:
    """The turn's limit as its requests carry it: counted from each one's dispatch."""
    if invocation.limit is None:
        return invocation.until
    return FromDispatch(limit=invocation.limit, watches=invocation.until.watches)


def _charges_authorized(
    state: SessionsState, context: SessionsContext, event: InvocationChargesAuthorized
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or invocation.phase != SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    owner = attempt_for(context, invocation.scope)
    if owner is None or not scope_active(context, invocation.scope):
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
    record = next(
        (
            row
            for row in context.records.records
            if getattr(row.request, "operation_id", None) == invocation.registered_operation
        ),
        None,
    )
    if (
        record is None
        or not isinstance(record.request, ExecuteRegisteredOperation)
        or record.phase in (RequestPhase.COMPLETED, RequestPhase.BLOCKED)
    ):
        raise ContractValidationError(
            "registered_operation", "missing unfinished canonical registered request"
        )
    return _registered_request(context, record.request, invocation.turn)


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
        or not scope_active(context, invocation.scope)
    ):
        return AreaChange(state=state)
    if invocation.turn.charge_class == "paid" and attempt_for(context, invocation.scope) is None:
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
    guidance = standing_guidance(state, context, invocation)
    request: Request
    if invocation.registered_operation is not None:
        request = _registered_dispatch(context, invocation)
        expected = tuple(
            ReservedInputOccurrence(input_id=item.input_id, artifact=item.artifact)
            for item in inputs
        )
        if not isinstance(request, ExecuteRegisteredOperation) or request.inputs != expected:
            raise ContractValidationError(
                "input_ids", "registered canonical manifest differs from reservation"
            )
    elif invocation.turn.charge_class == "resume":
        if invocation.turn.continuation_id is None:
            raise ContractValidationError(
                "continuation_id", "resume requires continuation identity"
            )
        _validate_successor(state, context, event.invocation, invocation.turn, invocation.scope)
        request = ResumeSessionTurn(
            request_id=turn_request_id(event.invocation, "dispatch"),
            scope=invocation.scope,
            admission_id=_episode(context, invocation.scope),
            turn=invocation.turn,
            inputs=inputs,
            guidance=guidance,
            continuation_id=invocation.turn.continuation_id,
            publication=_publication(context, invocation.turn.continuation_id),
        )
    else:
        request = DispatchTurn(
            request_id=turn_request_id(event.invocation, "dispatch"),
            scope=invocation.scope,
            admission_id=_episode(context, invocation.scope),
            turn=invocation.turn,
            inputs=inputs,
            guidance=guidance,
        )
    invocation = invocation.model_copy(
        update={
            "phase": SessionPhase.EXECUTING,
            "input_ids": event.input_ids,
            "guidance_ids": tuple(item.input_id for item in guidance),
            "reserved_inputs": tuple(item.artifact for item in inputs),
        }
    )
    session = _awaiting(
        session.model_copy(
            update={
                "phase": SessionPhase.EXECUTING,
                "pending_records": (*session.pending_records, request.request_id),
                "continuation_id": invocation.turn.continuation_id,
            }
        ),
        context,
        request,
    )
    state = _replace_session(_replace_invocation(state, invocation), session)
    return AreaChange(
        state=state, requests=(Outbound(request=request, until=_from_dispatch(invocation)),)
    )


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
