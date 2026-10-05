"""Pure acquisition, retained-checkpoint and receipt authority for attempts.

Retirement owns closure and release dependencies. This leaf only requests that
work through signals; canonical intents retain acquisition and write proofs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._adoption import fences_root_mutation
from ._evaluation_history import produce_history
from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    Verdict,
    accepted_receipt_for,
    admission_remaining,
    current_admission,
    current_closure,
    draining,
    invocation_for,
    observation_for,
    occupied_episode,
    operation_for,
)
from ._registry import ContractError
from .types.attempts import (
    AttemptAdmitted,
    AttemptChargeRefundRequested,
    AttemptCheckpoint,
    AttemptEvaluationHistoryUpdated,
    AttemptExhausted,
    AttemptPhase,
    AttemptReacquireRequested,
    AttemptRegistered,
    AttemptSetupFailed,
    AttemptsState,
    AttemptView,
    EnsureWorkspace,
    InitialSessionsFailed,
    InitialSessionsReady,
    InvocationChargeRequested,
    InvocationCheckpointed,
    InvocationCheckpointRequested,
    InvocationEnded,
    ReacquisitionReady,
    RestoreRevision,
    RetireRequested,
    RevisionOperationObserved,
    RevisionOperationRequested,
    SnapshotAndRetain,
    WorkspaceObserved,
)
from .types.common import (
    AttemptRef,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    ContractValidationError,
    DecisionId,
    ExecuteRegisteredOperation,
    InvocationRef,
    Observation,
    ObservationStatus,
    RequestBase,
    RequestId,
    RevisionAuthority,
    RunId,
    RunStatus,
    Scope,
    SetupFailureKind,
    WorkspaceMode,
    WorkspaceRef,
)
from .types.evaluation import ContinuationPhase
from .types.intents import InspectRequest, IntentPhase, IntentsState, RequestPrepared
from .types.kernel import AreaChange, DecisionReceipt
from .types.scheduling import AttemptReady
from .types.sessions import (
    Access,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    InvocationChargeRefunded,
    InvocationChargesAuthorized,
    InvocationCheckpointAvailable,
    ResumeSessionTurn,
    SessionPhase,
    SessionsAcquireRequested,
    SessionsState,
)
from .types.strategy import Operation, RequestTurn, StartAttempt

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent
    from .types.evaluation import Continuation
    from .types.intents import Intent
    from .types.kernel import AttemptsContext
    from .types.sessions import InterruptClaim, Invocation, SessionView


def _identity_mismatch(checks: tuple[tuple[ProofField, object, object], ...]) -> Mismatch | None:
    ordered = sorted(checks, key=lambda check: tuple(ProofField).index(check[0]))
    return next(
        (Mismatch(field) for field, actual, expected in ordered if actual != expected), None
    )


def _terminal_lease(invocation: Invocation, session: SessionView) -> Mismatch | None:
    ref = invocation.invocation
    return _identity_mismatch(
        (
            (ProofField.INVOCATION_ID, invocation.turn.invocation_id, ref.invocation_id),
            (ProofField.SESSION_ID, session.spec.session_id, ref.session_id),
            (ProofField.PAYLOAD, session.spec, invocation.turn.session),
            (
                ProofField.RESOURCE_ID,
                invocation.observation.resource_id
                if invocation.observation is not None
                and invocation.observation.resource_id is not None
                else session.resource_id,
                session.resource_id,
            ),
            (ProofField.GENERATION, session.generation, ref.generation),
        )
    )


def _request_turn_matches(invocation: Invocation, request: RequestBase) -> bool:
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return request.turn == invocation.turn
    if isinstance(request, InspectTurn):
        return request.invocation == invocation.invocation
    return (
        isinstance(request, ExecuteRegisteredOperation)
        and invocation.registered_operation is not None
        and request.operation_id == invocation.registered_operation
    )


def _terminal_request(
    invocation: Invocation, intents: IntentsState, observation: Observation, episode: DecisionId
) -> Verdict[Observation]:
    rows = tuple(row for row in intents.intents if row.request_id == observation.request_id)
    if not rows:
        return Missing(ProofReason.ABSENT_REQUEST)
    if len(rows) != 1:
        return Mismatch(ProofField.REQUEST_ID)
    intent = rows[0]
    if intent.observation is not None and intent.observation != observation:
        return Mismatch(ProofField.PAYLOAD)
    proof = observation_for(intent, observation)
    if not isinstance(proof, Proven):
        return proof
    matches = _request_turn_matches(invocation, intent.request)
    mismatch = _identity_mismatch(
        (
            (ProofField.SCOPE, observation.scope, invocation.scope),
            (ProofField.ADMISSION_ID, observation.admission_id, episode),
            (ProofField.PAYLOAD, matches, True),
        )
    )
    return mismatch if mismatch is not None else Proven(observation)


def exact_invocation_terminal(
    invocation: Invocation | None,
    session: SessionView | None,
    intents: IntentsState,
    episode: DecisionId | None,
) -> Verdict[Observation]:
    if invocation is None or session is None:
        return Missing(
            ProofReason.ABSENT_INVOCATION if invocation is None else ProofReason.ABSENT_SESSION
        )
    if episode is None:
        return Missing(ProofReason.ABSENT_EPISODE)
    observation = invocation.observation
    if observation is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    if observation.accepted and session.resource_id is None:
        return Missing(ProofReason.ABSENT_RESOURCE)
    mismatch = _terminal_lease(invocation, session)
    if mismatch is not None:
        return mismatch
    proof = _terminal_request(invocation, intents, observation, episode)
    return _resolved_terminal(proof)


def _resolved_terminal(proof: Verdict[Observation]) -> Verdict[Observation]:
    if not isinstance(proof, Proven):
        return proof
    observation = proof.value
    if (
        not observation.terminal
        or observation.status == ObservationStatus.PENDING
        or _unresolved_acceptance(observation)
    ):
        return Missing(ProofReason.UNRESOLVED)
    return proof


def _retained_session(
    attempt: AttemptView, continuation: Continuation, rows: tuple[SessionView, ...]
) -> Verdict[SessionView]:
    if not rows:
        return Missing(ProofReason.ABSENT_SESSION)
    if len(rows) != 1:
        return Mismatch(ProofField.SESSION_ID)
    row = rows[0]
    if row.resource_id is None:
        return Missing(ProofReason.ABSENT_RESOURCE)
    if (
        row.spec.session_id == continuation.invocation.session_id
        and row.generation != continuation.invocation.generation
    ):
        return Mismatch(ProofField.GENERATION)
    if row.scope != _scope(attempt) and (
        not isinstance(row.scope.owner, RunId)
        or row.spec.policy != "reuse"
        or row.spec.lifetime != "owner"
    ):
        return Mismatch(ProofField.SCOPE)
    mismatch = _identity_mismatch(
        (
            (
                ProofField.SESSION_ID,
                continuation.invocation.session_id,
                continuation.next_invocation.session_id,
            ),
            (
                ProofField.GENERATION,
                continuation.invocation.generation,
                continuation.next_invocation.generation,
            ),
        )
    )
    return mismatch if mismatch is not None else Proven(row)


def _retained_acquisition(
    attempt: AttemptView, session: SessionView, sessions: SessionsState, intents: IntentsState
) -> bool:
    if not any(
        group.attempt == _ref(attempt)
        and group.admission_id == attempt.admission_id
        and group.phase == "ready"
        and session.spec.session_id in group.session_ids
        for group in sessions.acquisition_groups
    ):
        return False
    return any(
        isinstance(row.request, EnsureSession)
        and row.request.spec == session.spec
        and row.request.required_resource == session.resource_id
        and row.request.scope == session.scope
        and row.request.admission_id == attempt.admission_id
        and row.phase == IntentPhase.COMPLETED
        and isinstance(observation_for(row, row.observation), Proven)
        and row.observation is not None
        and row.observation.resource_id == session.resource_id
        and _successful(row.observation)
        for row in intents.intents
    )


def retained_sessions_for(
    attempt: AttemptView | None,
    continuation: Continuation | None,
    sessions: SessionsState,
    intents: IntentsState,
) -> Verdict[tuple[SessionView, ...]]:
    if attempt is None or continuation is None:
        return Missing(ProofReason.ABSENT_INVOCATION)
    source = invocation_for(sessions.invocations, continuation.invocation, _scope(attempt))
    if not isinstance(source, Proven):
        return source
    required = tuple(dict.fromkeys((*attempt.sessions, continuation.invocation.session_id)))
    matched: list[SessionView] = []
    for identity in required:
        rows = tuple(row for row in sessions.sessions if row.spec.session_id == identity)
        proof = _retained_session(attempt, continuation, rows)
        if not isinstance(proof, Proven):
            return proof
        row = proof.value
        if identity == continuation.invocation.session_id and (
            row.spec != source.value.turn.session
            or (
                row.invocation != continuation.invocation.invocation_id
                and (
                    row.invocation is not None
                    or not _retained_acquisition(attempt, row, sessions, intents)
                )
            )
        ):
            return Mismatch(ProofField.INVOCATION_ID)
        matched.append(row)
    return Proven(tuple(matched))


def _ref(attempt: AttemptView) -> AttemptRef:
    return AttemptRef(attempt_id=attempt.attempt_id, generation=attempt.generation)


def _scope(attempt: AttemptView) -> Scope:
    return Scope(owner=attempt.attempt_id, generation=attempt.generation)


def _identity(attempt: AttemptView, purpose: str) -> str:
    # Length prefixes keep opaque attempt/episode roots unambiguous.
    owner = attempt.attempt_id.root
    episode = f"id:{attempt.admission_id.root}" if attempt.admission_id is not None else "none"
    return f"attempt:{len(owner)}:{owner}:{attempt.generation}:{len(episode)}:{episode}:{len(purpose)}:{purpose}"


def _closed(attempt: AttemptView) -> bool:
    return isinstance(current_closure(attempt, attempt.closure), Proven)


def _find(state: AttemptsState, ref: AttemptRef) -> AttemptView | None:
    return next((attempt for attempt in state.attempts if _ref(attempt) == ref), None)


def _replace(state: AttemptsState, attempt: AttemptView) -> AttemptsState:
    return state.model_copy(
        update={
            "attempts": tuple(
                attempt if _ref(previous) == _ref(attempt) else previous
                for previous in state.attempts
            )
        }
    )


def _intent(context: AttemptsContext, identity: RequestId) -> Intent | None:
    return next((row for row in context.intents.intents if row.request_id == identity), None)


def _fresh_observation(intent: Intent, observation: Observation) -> bool:
    return (
        intent.observation is None
        or observation.sequence > intent.observation.sequence
        or observation == intent.observation
    )


def _current(attempt: AttemptView, observation: Observation) -> bool:
    return isinstance(
        current_admission(attempt, observation.scope, observation.admission_id), Proven
    )


def _unresolved_acceptance(observation: Observation) -> bool:
    return observation.status == ObservationStatus.UNKNOWN or (
        observation.status == ObservationStatus.SUCCEEDED and not observation.accepted
    )


def _successful(observation: Observation) -> bool:
    return (
        observation.status == ObservationStatus.SUCCEEDED
        and observation.accepted
        and observation.terminal
    )


def _inspection(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, observation: Observation
) -> AreaChange[AttemptsState]:
    original = _intent(context, observation.request_id)
    if (
        original is None
        or original.request.scope != _scope(attempt)
        or original.request.admission_id != attempt.admission_id
        or not _fresh_observation(original, observation)
    ):
        return AreaChange(state=state)
    identity = RequestId(root=f"inspect:{observation.request_id.root}:{observation.sequence}")
    if _intent(context, identity) is not None:
        return AreaChange(state=state)
    return AreaChange(
        state=state,
        requests=(
            InspectRequest(
                request_id=identity,
                scope=_scope(attempt),
                admission_id=attempt.admission_id,
                deadline_at=min(
                    context.run.deadline_at,
                    context.run.now_at + context.run.limits.reconciliation_bound,
                ),
                target=observation.request_id,
                # Root lease identity remains in the original observation.
                resource_id=None,
            ),
        ),
    )


def _register(state: AttemptsState, event: AttemptRegistered | AttemptAdmitted) -> AttemptView:
    ref = AttemptRef(attempt_id=event.request.attempt_id, generation=event.request.generation)
    session_ids = tuple(spec.session_id for spec in event.initial_sessions)
    if len(set(session_ids)) != len(session_ids):
        raise ContractValidationError("initial_sessions", "duplicate session identity")
    previous = _find(state, ref)
    if previous is not None:
        if (
            previous.item_id != event.request.item_id
            or previous.workspace != event.workspace
            or previous.budget != event.budget
            or previous.sessions != session_ids
            or not any(
                charge.charge_id == ChargeId(root=f"admission:{event.request.decision_id.root}")
                and charge.charged == event.request.admission_charge
                for charge in previous.charges
            )
        ):
            raise ContractValidationError("attempt_id", "conflicting attempt registration")
        return previous
    if any(row.attempt_id == ref.attempt_id for row in state.attempts):
        raise ContractValidationError("generation", "attempt identity already registered")
    if event.request.admission_charge != event.budget.admission_charge:
        raise ContractValidationError("admission_charge", "request and budget disagree")
    predecessor = event.workspace.parked_predecessor
    if predecessor is not None:
        parent = _find(state, predecessor)
        if (
            parent is None
            or parent.phase != AttemptPhase.PARKED
            or parent.closure is None
            or parent.closure.disposition != "park"
            or parent.checkpoint != event.workspace.base
            or parent.pending_intents
            or parent.release_dependencies
        ):
            raise ContractValidationError("parked_predecessor", "parked checkpoint proof required")
    return AttemptView(
        attempt_id=ref.attempt_id,
        item_id=event.request.item_id,
        generation=ref.generation,
        phase=AttemptPhase.QUEUED,
        workspace=event.workspace,
        budget=event.budget,
        parent=predecessor,
        sessions=session_ids,
        charges=(
            ChargeReceipt(
                charge_id=ChargeId(root=f"admission:{event.request.decision_id.root}"),
                kind=ChargeKind.ADMISSION,
                charged=event.request.admission_charge,
            ),
        ),
    )


def _root_conflict(state: AttemptsState, context: AttemptsContext, attempt: AttemptView) -> bool:
    """Defensive recheck of the gate in Scheduling: the root has another owner or an adoption."""
    if attempt.workspace.mode != WorkspaceMode.EXCLUSIVE_ROOT:
        return False
    return fences_root_mutation(context.settlement, context.intents) or any(
        _ref(other) != _ref(attempt)
        and other.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT
        and (
            other.phase not in (AttemptPhase.QUEUED, AttemptPhase.TERMINAL, AttemptPhase.PARKED)
            or other.release_dependencies
            or other.pending_intents
        )
        for other in state.attempts
    )


def _registered(state: AttemptsState, attempt: AttemptView) -> AttemptsState:
    if _find(state, _ref(attempt)) is not None:
        return state
    return state.model_copy(update={"attempts": (*state.attempts, attempt)})


def _decline_root(
    attempt: AttemptView, context: AttemptsContext, admission_id: DecisionId
) -> RetireRequested:
    return RetireRequested(
        attempt=_ref(attempt),
        disposition="cancel",
        authority=RequestId(root=f"root-conflict:{admission_id.root}"),
        admission_id=admission_id,
        requested_at=context.run.now_at,
    )


def _admit(
    state: AttemptsState, context: AttemptsContext, event: AttemptAdmitted
) -> AreaChange[AttemptsState]:
    attempt = _register(state, event)
    if attempt.phase != AttemptPhase.QUEUED or _closed(attempt):
        return AreaChange(state=state)
    if event.admission_id != event.request.decision_id:
        raise ContractValidationError("admission_id", "initial admission must match registration")
    if not isinstance(occupied_episode(context.scheduling.slots, event.request), Proven):
        return AreaChange(state=state)
    if (
        not (context.run.status == RunStatus.RUNNING or draining(context.run))
        or context.run.now_at >= context.run.deadline_at
    ):
        registered = (
            state
            if _find(state, _ref(attempt)) is not None
            else state.model_copy(update={"attempts": (*state.attempts, attempt)})
        )
        return AreaChange(
            state=registered,
            signals=(
                RetireRequested(
                    attempt=_ref(attempt),
                    disposition="cancel",
                    authority=RequestId(root=f"admission-closed:{event.admission_id.root}"),
                    admission_id=event.admission_id,
                    requested_at=context.run.now_at,
                ),
            ),
        )
    if _root_conflict(state, context, attempt):
        # Scheduling's gate should have prevented this. Decline by cancelling the
        # admission, which frees its slot; never fail the step on an internal signal.
        return AreaChange(
            state=_registered(state, attempt),
            signals=(_decline_root(attempt, context, event.admission_id),),
        )
    attempt = attempt.model_copy(
        update={"phase": AttemptPhase.ACQUIRING, "admission_id": event.admission_id}
    )
    identity = RequestId(root=_identity(attempt, "workspace"))
    attempt = attempt.model_copy(
        update={
            "pending_intents": (*attempt.pending_intents, identity),
            "sessions": tuple(spec.session_id for spec in event.initial_sessions),
        }
    )
    registered = (
        state
        if _find(state, _ref(attempt)) is not None
        else state.model_copy(update={"attempts": (*state.attempts, attempt)})
    )
    return AreaChange(
        state=_replace(registered, attempt),
        requests=(
            EnsureWorkspace(
                request_id=identity,
                scope=_scope(attempt),
                admission_id=attempt.admission_id,
                deadline_at=context.run.deadline_at,
                attempt=_ref(attempt),
                plan=attempt.workspace,
            ),
        ),
        signals=(
            SessionsAcquireRequested(
                attempt=_ref(attempt),
                admission_id=event.admission_id,
                scope=_scope(attempt),
                specs=event.initial_sessions,
            ),
        )
        if event.initial_sessions
        else (),
    )


def _workspace_request_matches(attempt: AttemptView, intent: Intent) -> bool:
    request = intent.request
    if isinstance(request, EnsureWorkspace):
        payload_matches = request.plan == attempt.workspace
    elif isinstance(request, RestoreRevision):
        payload_matches = request.revision == attempt.checkpoint
    else:
        return False
    return (
        payload_matches
        and request.attempt == _ref(attempt)
        and request.scope == _scope(attempt)
        and request.admission_id == attempt.admission_id
    )


def _ready(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView
) -> AreaChange[AttemptsState]:
    if attempt.phase != AttemptPhase.ACQUIRING or attempt.admission_id is None or _closed(attempt):
        return AreaChange(state=state)
    group = next(
        (
            group
            for group in context.sessions.acquisition_groups
            if group.attempt == _ref(attempt) and group.admission_id == attempt.admission_id
        ),
        None,
    )
    if attempt.sessions and (
        group is None
        or group.phase != "ready"
        or not set(attempt.sessions).issubset(group.session_ids)
    ):
        return AreaChange(state=state)
    if any(
        isinstance(row.request, EnsureWorkspace | RestoreRevision)
        and row.request.scope == _scope(attempt)
        and row.request.admission_id == attempt.admission_id
        and row.request_id in attempt.pending_intents
        for row in context.intents.intents
    ):
        return AreaChange(state=state)
    # A retained canonical observation is the durable workspace readiness proof.
    proofs = tuple(
        row
        for row in context.intents.intents
        if isinstance(row.request, EnsureWorkspace | RestoreRevision)
        and row.request.scope == _scope(attempt)
        and row.request.admission_id == attempt.admission_id
    )
    if not proofs or any(
        not _workspace_request_matches(attempt, row)
        or row.phase != IntentPhase.COMPLETED
        or row.observation is None
        or not _current(attempt, row.observation)
        or row.observation.request_id != row.request_id
        or not _successful(row.observation)
        for row in proofs
    ):
        return AreaChange(state=state)
    if any(isinstance(row.request, RestoreRevision) for row in proofs):
        return _reacquisition_ready(state, context, attempt, proofs)
    active = attempt.model_copy(update={"phase": AttemptPhase.ACTIVE})
    return AreaChange(
        state=_replace(state, active),
        signals=(AttemptReady(attempt=_ref(active), admission_id=attempt.admission_id),),
    )


def _reacquisition_ready(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, proofs: tuple[Intent, ...]
) -> AreaChange[AttemptsState]:
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.phase == ContinuationPhase.REOPENING
            and row.reopen_authority is not None
            and _invocation(context, attempt, row.invocation) is not None
            and row.next_invocation.session_id == row.invocation.session_id
            and row.next_invocation.generation == row.invocation.generation
            and any(
                proof.request_id
                == RequestId(root=_identity(attempt, f"restore:{row.reopen_authority.root}"))
                for proof in proofs
            )
        ),
        None,
    )
    if (
        continuation is None
        or continuation.reopen_authority is None
        or attempt.admission_id is None
    ):
        return AreaChange(state=state)
    retained = _retained(context, attempt, continuation)
    if not isinstance(retained, Proven) or not all(
        _retained_acquisition(attempt, row, context.sessions, context.intents)
        for row in retained.value
    ):
        return AreaChange(state=state)
    required = tuple(row.spec.session_id for row in retained.value)
    if not any(
        group.attempt == _ref(attempt)
        and group.admission_id == attempt.admission_id
        and group.phase == "ready"
        and group.session_ids == required
        for group in context.sessions.acquisition_groups
    ):
        return AreaChange(state=state)
    return AreaChange(
        state=state,
        signals=(
            ReacquisitionReady(
                attempt=_ref(attempt),
                request_id=continuation.reopen_authority,
                admission_id=attempt.admission_id,
            ),
        ),
    )


def _workspace(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: WorkspaceObserved
) -> AreaChange[AttemptsState]:
    observation = event.observation
    intent = _intent(context, observation.request_id)
    if (
        not _current(attempt, observation)
        or intent is None
        or intent.request_id not in attempt.pending_intents
        or not _fresh_observation(intent, observation)
    ):
        return AreaChange(state=state)
    if (
        not isinstance(intent.request, EnsureWorkspace | RestoreRevision)
        or not _workspace_request_matches(attempt, intent)
        or intent.request.scope != _scope(attempt)
        or intent.request.admission_id != attempt.admission_id
    ):
        return AreaChange(state=state)
    if _unresolved_acceptance(observation):
        return _inspection(state, context, attempt, observation)
    if (
        observation.status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
        and observation.terminal
    ):
        return _setup_failed(state, context, attempt, observation, SetupFailureKind.TRANSIENT)
    expected = (
        intent.request.plan.base
        if isinstance(intent.request, EnsureWorkspace)
        else intent.request.revision
    )
    if (
        _closed(attempt)
        or attempt.phase != AttemptPhase.ACQUIRING
        or not _successful(observation)
        or intent.phase != IntentPhase.COMPLETED
        or intent.observation != observation
        or event.revision != expected
    ):
        return AreaChange(state=state)
    updated = attempt.model_copy(
        update={
            "pending_intents": tuple(
                identity
                for identity in attempt.pending_intents
                if identity != observation.request_id
            )
        }
    )
    state = _replace(state, updated)
    return _ready(state, context, updated)


def _setup_origin(context: AttemptsContext, attempt: AttemptView, observation: Observation) -> bool:
    intent = _intent(context, observation.request_id)
    if (
        intent is None
        or intent.request.scope != _scope(attempt)
        or intent.request.admission_id != attempt.admission_id
        or not _fresh_observation(intent, observation)
    ):
        return False
    if isinstance(intent.request, EnsureWorkspace | RestoreRevision):
        return intent.request_id in attempt.pending_intents and intent.request.attempt == _ref(
            attempt
        )
    if isinstance(intent.request, EnsureSession):
        return any(
            group.attempt == _ref(attempt)
            and group.admission_id == attempt.admission_id
            and group.failure_request == observation.request_id
            and group.phase == "failed"
            and intent.request.spec.session_id in group.session_ids
            for group in context.sessions.acquisition_groups
        )
    return False


def _setup_failed(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    observation: Observation,
    failure: SetupFailureKind,
) -> AreaChange[AttemptsState]:
    if not _current(attempt, observation) or not _setup_origin(context, attempt, observation):
        return AreaChange(state=state)
    if _unresolved_acceptance(observation) or (
        failure == SetupFailureKind.UNKNOWN and not observation.terminal
    ):
        return _inspection(state, context, attempt, observation)
    if (
        _closed(attempt)
        or attempt.phase != AttemptPhase.ACQUIRING
        or attempt.admission_id is None
        or not observation.terminal
        or observation.status
        not in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        return AreaChange(state=state)
    identity = ChargeId(root=_identity(attempt, "setup"))
    if any(charge.charge_id == identity for charge in attempt.charges):
        return AreaChange(state=state)
    # Episode-free receipts cannot prove another setup debit after reopening.
    paid = any(
        charge.kind == ChargeKind.ATTEMPT and charge.invocation_id is not None
        for charge in attempt.charges
    )
    spent = sum(
        charge.charged - charge.refunded
        for charge in attempt.charges
        if charge.kind == ChargeKind.ATTEMPT
    )
    exhausted = not paid and spent >= attempt.budget.paid_invocation_limit
    charges = (
        attempt.charges
        if paid or exhausted
        else (
            *attempt.charges,
            ChargeReceipt(
                charge_id=identity,
                kind=ChargeKind.ATTEMPT,
                source_request=observation.request_id,
                charged=1,
            ),
        )
    )
    updated = attempt.model_copy(update={"charges": charges})
    return AreaChange(
        state=_replace(state, updated),
        events=(AttemptExhausted(attempt=_ref(attempt), reason="paid-limit"),) if exhausted else (),
        signals=(
            RetireRequested(
                attempt=_ref(attempt),
                disposition="cancel",
                authority=RequestId(root=_identity(attempt, "setup-retirement")),
                admission_id=attempt.admission_id,
                requested_at=context.run.now_at,
            ),
        ),
    )


def _invocation(
    context: AttemptsContext, attempt: AttemptView, ref: InvocationRef
) -> Invocation | None:
    proof = invocation_for(context.sessions.invocations, ref, _scope(attempt))
    return proof.value if isinstance(proof, Proven) else None


def _session_scope(context: AttemptsContext, attempt: AttemptView, session: SessionView) -> bool:
    return session.scope in (
        _scope(attempt),
        Scope(owner=context.run.run_id, generation=context.run.generation),
    )


def _session(
    context: AttemptsContext, attempt: AttemptView, ref: InvocationRef
) -> SessionView | None:
    rows = tuple(
        row
        for row in context.sessions.sessions
        if row.spec.session_id == ref.session_id
        and row.generation == ref.generation
        and _session_scope(context, attempt, row)
    )
    return rows[0] if len(rows) == 1 else None


def _retained(
    context: AttemptsContext, attempt: AttemptView, continuation: Continuation
) -> Verdict[tuple[SessionView, ...]]:
    proof = retained_sessions_for(attempt, continuation, context.sessions, context.intents)
    if isinstance(proof, Proven) and not all(
        _session_scope(context, attempt, row) for row in proof.value
    ):
        return Mismatch(ProofField.SCOPE)
    return proof


def _terminal(context: AttemptsContext, invocation: Invocation, attempt: AttemptView) -> bool:
    session = _session(context, attempt, invocation.invocation)
    return _billing_origin(context, attempt, invocation) and isinstance(
        exact_invocation_terminal(invocation, session, context.intents, attempt.admission_id),
        Proven,
    )


def _correction_allowed(
    context: AttemptsContext, attempt: AttemptView, invocation: Invocation
) -> bool:
    predecessor = invocation.turn.predecessor
    seen = {invocation.invocation}
    depth = 0
    child = invocation
    while predecessor is not None:
        if predecessor in seen:
            return False
        seen.add(predecessor)
        previous = _invocation(context, attempt, predecessor)
        if previous is None or not _terminal(context, previous, attempt):
            return False
        if _completed_interruption(context, attempt, child) is not True:
            depth += 1
        child = previous
        predecessor = previous.turn.predecessor if previous.turn.charge_class != "resume" else None
    return depth > 0 and depth <= min(attempt.budget.retry_limit, context.run.limits.max_retries)


def _initial_session_acquisition(
    context: AttemptsContext, session: SessionView, ref: InvocationRef
) -> bool:
    # Sessions publishes acquisition IDs before quiescent outbox registration.
    return (
        session.phase == SessionPhase.ACQUIRING
        and session.invocation == ref.invocation_id
        and bool(session.pending_intents)
        and not any(
            row.invocation.session_id == ref.session_id and row.invocation != ref
            for row in context.sessions.invocations
        )
    )


def _resume_source(
    context: AttemptsContext, attempt: AttemptView, invocation: Invocation
) -> Invocation | None:
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.continuation_id == invocation.turn.continuation_id
            and row.next_invocation == invocation.invocation
            and row.phase == ContinuationPhase.AUTHORIZED
            and invocation.turn.predecessor in (None, row.invocation)
            and row.invocation.session_id == invocation.invocation.session_id
        ),
        None,
    )
    return (
        _invocation(context, attempt, continuation.invocation) if continuation is not None else None
    )


def _billing_origin(context: AttemptsContext, attempt: AttemptView, invocation: Invocation) -> bool:
    origins = tuple(
        row
        for row in context.run.receipts
        if (
            (isinstance(row.decision, RequestTurn) and row.decision.turn == invocation.turn)
            or (
                isinstance(row.decision, Operation)
                and row.decision.registered_turn == invocation.turn
            )
        )
        and row.decision.scope == _scope(attempt)
        and isinstance(
            accepted_receipt_for(context.run.receipts, row.decision_id, row.decision), Proven
        )
    )
    return len(origins) == 1 and (
        (invocation.phase == SessionPhase.ACQUIRING and invocation.observation is None)
        or any(
            _request_turn_matches(invocation, row.request)
            and (
                not isinstance(row.request, ExecuteRegisteredOperation)
                or isinstance(operation_for(context.run.receipts, row.request), Proven)
            )
            and row.request.scope == _scope(attempt)
            and row.request.admission_id == attempt.admission_id
            and row.request.decision_id == origins[0].decision_id
            and row.request_id in origins[0].request_ids
            for row in context.intents.intents
        )
    )


def _chargeable_invocation(
    context: AttemptsContext, attempt: AttemptView, ref: InvocationRef
) -> Invocation | None:
    invocation = _invocation(context, attempt, ref)
    if (
        attempt.phase != AttemptPhase.ACTIVE
        or not isinstance(current_admission(attempt, _scope(attempt), attempt.admission_id), Proven)
        or _closed(attempt)
        or invocation is None
        or invocation.phase in (SessionPhase.UNKNOWN, SessionPhase.CLOSING, SessionPhase.TERMINAL)
        or (
            invocation.observation is not None
            and (
                not _current(attempt, invocation.observation)
                or _unresolved_acceptance(invocation.observation)
                or not isinstance(
                    observation_for(
                        _intent(context, invocation.observation.request_id), invocation.observation
                    ),
                    Proven,
                )
            )
        )
        or invocation.turn.invocation_id != ref.invocation_id
        or invocation.turn.session.session_id != ref.session_id
        or (
            attempt.workspace.mode == WorkspaceMode.READ_ONLY_REVISION
            and invocation.turn.session.access == Access.WRITE_CANDIDATE
        )
        or (
            invocation.turn.workspace.scope
            if isinstance(invocation.turn.workspace, WorkspaceRef)
            else invocation.turn.workspace
        )
        != _scope(attempt)
        or (
            isinstance(invocation.turn.workspace, WorkspaceRef)
            and invocation.turn.workspace.mode != attempt.workspace.mode
        )
        or any(charge.invocation_id == ref.invocation_id for charge in attempt.charges)
    ):
        return None
    if not _billing_origin(context, attempt, invocation):
        return None
    session = _session(context, attempt, ref)
    if (
        session is None
        or session.spec != invocation.turn.session
        or session.phase
        not in (
            SessionPhase.ACQUIRING,
            SessionPhase.IDLE,
            SessionPhase.EXECUTING,
            SessionPhase.CHECKPOINTED,
            SessionPhase.SUSPENDED,
        )
        or (
            session.phase == SessionPhase.ACQUIRING
            and (
                invocation.phase != SessionPhase.ACQUIRING
                or not _initial_session_acquisition(context, session, ref)
            )
        )
        or (session.phase == SessionPhase.SUSPENDED and invocation.turn.charge_class != "resume")
        or session.invocation not in (None, ref.invocation_id)
        or (
            session.spec.policy == "reuse"
            and session.resource_id is None
            and not _initial_session_acquisition(context, session, ref)
        )
    ):
        return None
    return invocation


def _completed_interruption(
    context: AttemptsContext, attempt: AttemptView, invocation: Invocation
) -> bool | None:
    predecessor = invocation.turn.predecessor
    if invocation.turn.charge_class not in ("paid", "free") or predecessor is None:
        return None
    claims = tuple(
        claim for claim in context.sessions.interrupts if claim.invocation == predecessor
    )
    if not claims:
        return None
    previous = _invocation(context, attempt, predecessor)
    if len(claims) != 1 or previous is None or not _terminal(context, previous, attempt):
        return False
    claim = claims[0]
    checkpoint = any(
        row.invocation == predecessor
        and row.request_id == claim.checkpoint_authority
        and row.retention == "wip"
        for row in attempt.checkpoints
    )
    if claim.phase != "completed" or not checkpoint:
        return False
    if claim.refund == 0:
        return True
    return any(
        row.charge_id == claim.refunded_charge
        and row.kind == ChargeKind.ATTEMPT
        and row.invocation_id == predecessor.invocation_id
        and row.historical_proof is None
        and row.refunded >= claim.refund
        and claim.authority in row.refund_sources
        for row in attempt.charges
    )


def _charge(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: InvocationChargeRequested,
) -> AreaChange[AttemptsState]:
    invocation = _chargeable_invocation(context, attempt, event.invocation)
    replacement = (
        _completed_interruption(context, attempt, invocation) if invocation is not None else None
    )
    if (
        invocation is None
        or replacement is False
        or any(
            other.attempt_id != attempt.attempt_id
            and charge.invocation_id == event.invocation.invocation_id
            for other in state.attempts
            for charge in other.charges
        )
    ):
        return AreaChange(state=state)
    turn_usage = sum(
        charge.charged
        for owner in state.attempts
        for charge in owner.charges
        if charge.kind == ChargeKind.TURN
    ) + sum(
        charge.charged for charge in context.sessions.run_charges if charge.kind == ChargeKind.TURN
    )
    if turn_usage >= context.run.limits.max_turns:
        return AreaChange(state=state)
    charge_class = invocation.turn.charge_class
    if (
        charge_class != "resume"
        and replacement is not True
        and (charge_class == "correction" or invocation.turn.predecessor is not None)
    ) and not _correction_allowed(context, attempt, invocation):
        return AreaChange(
            state=state, events=(AttemptExhausted(attempt=_ref(attempt), reason="retry-limit"),)
        )
    if charge_class == "resume" and _resume_source(context, attempt, invocation) is None:
        return AreaChange(state=state)
    spent = sum(
        charge.charged - charge.refunded
        for charge in attempt.charges
        if charge.kind == ChargeKind.ATTEMPT
    )
    if charge_class == "paid" and spent >= attempt.budget.paid_invocation_limit:
        return AreaChange(
            state=state, events=(AttemptExhausted(attempt=_ref(attempt), reason="paid-limit"),)
        )
    charges = (
        ChargeReceipt(
            charge_id=ChargeId(root=f"turn:{event.invocation.invocation_id.root}"),
            kind=ChargeKind.TURN,
            invocation_id=event.invocation.invocation_id,
            charged=1,
        ),
    )
    if charge_class == "paid":
        charges = (
            *charges,
            ChargeReceipt(
                charge_id=ChargeId(root=f"paid:{event.invocation.invocation_id.root}"),
                kind=ChargeKind.ATTEMPT,
                invocation_id=event.invocation.invocation_id,
                charged=1,
            ),
        )
    updated = attempt.model_copy(update={"charges": (*attempt.charges, *charges)})
    return AreaChange(
        state=_replace(state, updated),
        signals=(
            InvocationChargesAuthorized(
                invocation=event.invocation,
                charge_ids=tuple(charge.charge_id for charge in charges),
            ),
        ),
    )


def _interrupt_checkpoint(
    context: AttemptsContext, invocation: InvocationRef, identity: RequestId
) -> bool:
    claims = tuple(
        claim
        for claim in context.sessions.interrupts
        if claim.checkpoint_authority == identity
        or (
            claim.checkpoint_authority is None
            and claim.authority == identity
            and claim.phase in ("pending", "draining")
        )
    )
    return (
        len(claims) == 1
        and claims[0].invocation == invocation
        and claims[0].phase in ("pending", "draining", "checkpointed")
    )


def _checkpoint_request(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: InvocationCheckpointRequested,
) -> AreaChange[AttemptsState]:
    invocation = _invocation(context, attempt, event.invocation)
    if (
        invocation is None
        or not _terminal(context, invocation, attempt)
        or attempt.phase not in (AttemptPhase.ACTIVE, AttemptPhase.CLOSING)
        or attempt.admission_id is None
    ):
        return AreaChange(state=state)
    if not any(
        charge.invocation_id == event.invocation.invocation_id and charge.historical_proof is None
        for charge in attempt.charges
    ):
        return AreaChange(state=state)
    identity = event.authority
    if (
        any(checkpoint.request_id == identity for checkpoint in attempt.checkpoints)
        or identity in attempt.pending_intents
    ):
        return AreaChange(state=state)
    if any(
        row.scope == _scope(attempt)
        and row.invocation != event.invocation
        and row.turn.session.access == Access.WRITE_CANDIDATE
        and not _terminal(context, row, attempt)
        for row in context.sessions.invocations
    ):
        return AreaChange(state=state)
    # Only interruption claims authorize an attempt-owned invocation checkpoint, and an
    # interruption retains work in progress ("wip"): every later proof (Checkpointed, the
    # session claim, the retention check) accepts nothing else. Anything else is declined.
    if event.retention != "wip" or not _interrupt_checkpoint(context, event.invocation, identity):
        return AreaChange(state=state)
    request = SnapshotAndRetain(
        request_id=identity,
        scope=_scope(attempt),
        admission_id=attempt.admission_id,
        deadline_at=context.run.deadline_at,
        attempt=_ref(attempt),
        retention=event.retention,
        invocation=event.invocation,
    )
    updated = attempt.model_copy(update={"pending_intents": (*attempt.pending_intents, identity)})
    return AreaChange(state=_replace(state, updated), requests=(request,))


def _checkpointed(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: InvocationCheckpointed,
) -> AreaChange[AttemptsState]:
    if any(row.request_id == event.checkpoint_request for row in attempt.checkpoints):
        return AreaChange(state=state)
    invocation = _invocation(context, attempt, event.invocation)
    intent = _intent(context, event.checkpoint_request)
    if (
        invocation is None
        or not _terminal(context, invocation, attempt)
        or event.charge not in attempt.charges
        or event.charge.invocation_id != event.invocation.invocation_id
        or event.charge.historical_proof is not None
        or not _interrupt_checkpoint(context, event.invocation, event.checkpoint_request)
    ):
        return AreaChange(state=state)
    if (
        intent is None
        or not isinstance(intent.request, SnapshotAndRetain)
        or intent.request.scope != _scope(attempt)
        or intent.request.attempt != _ref(attempt)
        or intent.request.retention != "wip"
        or intent.request.invocation != event.invocation
        or intent.request.admission_id != attempt.admission_id
        or intent.request_id not in attempt.pending_intents
        or intent.phase != IntentPhase.COMPLETED
        or intent.observation is None
        or intent.observation.request_id != event.checkpoint_request
        or not _current(attempt, intent.observation)
        or not _successful(intent.observation)
        or intent.observation.revision != event.revision
    ):
        return AreaChange(state=state)
    checkpoint = AttemptCheckpoint(
        invocation=event.invocation,
        request_id=event.checkpoint_request,
        revision=event.revision,
        retention=intent.request.retention,
    )
    updated = attempt.model_copy(
        update={
            "checkpoints": (*attempt.checkpoints, checkpoint),
            "pending_intents": tuple(
                identity
                for identity in attempt.pending_intents
                if identity != event.checkpoint_request
            ),
        }
    )
    return AreaChange(
        state=_replace(state, updated),
        signals=(
            InvocationCheckpointAvailable(
                invocation=event.invocation,
                request_id=checkpoint.request_id,
                revision=checkpoint.revision,
                retention=checkpoint.retention,
            ),
        ),
    )


def _receipt_invocation(
    context: AttemptsContext, attempt: AttemptView, charge: ChargeReceipt
) -> Invocation | None:
    matches = tuple(
        row
        for row in context.sessions.invocations
        if row.invocation.invocation_id == charge.invocation_id
    )
    return _invocation(context, attempt, matches[0].invocation) if len(matches) == 1 else None


def _refund_proof(
    context: AttemptsContext,
    attempt: AttemptView,
    charge: ChargeReceipt,
    event: AttemptChargeRefundRequested,
) -> Invocation | None:
    invocation = _receipt_invocation(context, attempt, charge)
    if invocation is None or not _terminal(context, invocation, attempt):
        return None
    claim = next(
        (
            row
            for row in context.sessions.interrupts
            if row.invocation == invocation.invocation and row.authority == event.authority
        ),
        None,
    )
    checkpoint = next(
        (
            row
            for row in attempt.checkpoints
            if row.invocation == invocation.invocation
            and row.request_id == event.checkpoint_authority
        ),
        None,
    )
    proof = refund_for(attempt.charges, event, claim, checkpoint, invocation)
    return invocation if isinstance(proof, Proven) else None


def refund_for(
    charges: tuple[ChargeReceipt, ...],
    request: AttemptChargeRefundRequested,
    claim: InterruptClaim | None,
    checkpoint: AttemptCheckpoint | None,
    invocation: Invocation | None,
) -> Verdict[ChargeReceipt]:
    rows = tuple(row for row in charges if row.charge_id == request.charge_id)
    if len(rows) != 1:
        return Mismatch(ProofField.RECEIPT_ID)
    if claim is None or checkpoint is None or invocation is None:
        return Missing(ProofReason.ABSENT_INVOCATION)
    charge = rows[0]
    mismatch = _identity_mismatch(
        (
            (ProofField.INVOCATION_ID, charge.invocation_id, invocation.invocation.invocation_id),
            (ProofField.INVOCATION_ID, claim.invocation, invocation.invocation),
            (ProofField.INVOCATION_ID, checkpoint.invocation, invocation.invocation),
            (ProofField.REQUEST_ID, claim.authority, request.authority),
            (ProofField.REQUEST_ID, claim.checkpoint_authority, request.checkpoint_authority),
            (ProofField.REQUEST_ID, checkpoint.request_id, request.checkpoint_authority),
            (ProofField.PAYLOAD, claim.refund, request.amount),
            (ProofField.PAYLOAD, claim.refunded_charge, None),
            (ProofField.STATUS, claim.phase, "checkpointed"),
            (ProofField.STATUS, checkpoint.retention, "wip"),
        )
    )
    return mismatch if mismatch is not None else Proven(charge)


def _refund(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: AttemptChargeRefundRequested,
) -> AreaChange[AttemptsState]:
    charge = next((row for row in attempt.charges if row.charge_id == event.charge_id), None)
    # REJECTED cannot prove unsupported capability without persisted classification.
    if (
        charge is None
        or event.reason != "interrupted"
        or charge.kind == ChargeKind.TURN
        or charge.historical_proof is not None
        or any(
            event.authority in row.refund_sources
            for owner in state.attempts
            for row in owner.charges
        )
        or any(event.authority in row.refund_sources for row in context.sessions.run_charges)
        or event.amount == 0
        or event.amount > charge.charged - charge.refunded
    ):
        return AreaChange(state=state)
    invocation = _refund_proof(context, attempt, charge, event)
    if charge.kind != ChargeKind.ATTEMPT or invocation is None:
        return AreaChange(state=state)
    refunded = sum(row.refunded for owner in state.attempts for row in owner.charges) + sum(
        row.refunded for row in context.sessions.run_charges
    )
    if refunded + event.amount > context.run.limits.max_refunds:
        return AreaChange(
            state=state, events=(AttemptExhausted(attempt=_ref(attempt), reason="refund-limit"),)
        )
    if (
        charge.kind == ChargeKind.ATTEMPT
        and sum(row.refunded for row in attempt.charges if row.kind == ChargeKind.ATTEMPT)
        + event.amount
        > attempt.budget.refund_limit
    ):
        return AreaChange(
            state=state, events=(AttemptExhausted(attempt=_ref(attempt), reason="refund-limit"),)
        )
    receipt = charge.model_copy(
        update={
            "refunded": charge.refunded + event.amount,
            "refund_sources": (*charge.refund_sources, event.authority),
        }
    )
    updated = attempt.model_copy(
        update={
            "charges": tuple(
                receipt if row.charge_id == receipt.charge_id else row for row in attempt.charges
            )
        }
    )
    return AreaChange(
        state=_replace(state, updated),
        signals=(
            InvocationChargeRefunded(
                invocation=invocation.invocation,
                charge_id=charge.charge_id,
                authority=event.authority,
            ),
        )
        if invocation is not None and event.reason == "interrupted"
        else (),
    )


def _reacquire(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: AttemptReacquireRequested,
) -> AreaChange[AttemptsState]:
    if (
        attempt.phase != AttemptPhase.ACQUIRING
        or _closed(attempt)
        or not isinstance(current_admission(attempt, _scope(attempt), event.admission_id), Proven)
        or attempt.admission_id != event.admission_id
        or attempt.checkpoint != event.base
    ):
        return AreaChange(state=state)
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.continuation_id == event.continuation_id
        ),
        None,
    )
    if (
        continuation is None
        or continuation.phase != ContinuationPhase.REOPENING
        or continuation.reopen_authority != event.request_id
        or _invocation(context, attempt, continuation.invocation) is None
        or continuation.next_invocation.session_id != continuation.invocation.session_id
        or continuation.next_invocation.generation != continuation.invocation.generation
    ):
        return AreaChange(state=state)
    proof = _retained(context, attempt, continuation)
    if not isinstance(proof, Proven):
        return AreaChange(state=state)
    sessions = proof.value
    conflict = _root_conflict(state, context, attempt)
    if conflict or (
        attempt.workspace.mode == WorkspaceMode.READ_ONLY_REVISION
        and event.base != attempt.workspace.base
    ):
        # A root conflict declines by cancelling the admission; never fail the step.
        decline = (_decline_root(attempt, context, event.admission_id),) if conflict else ()
        return AreaChange(state=state, signals=decline)
    identity = RequestId(root=_identity(attempt, f"restore:{event.request_id.root}"))
    if identity in attempt.pending_intents or _intent(context, identity) is not None:
        return AreaChange(state=state)
    updated = attempt.model_copy(update={"pending_intents": (*attempt.pending_intents, identity)})
    return AreaChange(
        state=_replace(state, updated),
        requests=(
            RestoreRevision(
                request_id=identity,
                scope=_scope(attempt),
                admission_id=attempt.admission_id,
                deadline_at=context.run.deadline_at,
                attempt=_ref(attempt),
                revision=event.base,
            ),
        ),
        signals=(
            SessionsAcquireRequested(
                attempt=_ref(attempt),
                admission_id=event.admission_id,
                scope=_scope(attempt),
                specs=tuple(row.spec for row in sessions),
            ),
        )
        if sessions
        else (),
    )


def _revision_request(
    state: AttemptsState, context: AttemptsContext, event: RevisionOperationRequested
) -> AreaChange[AttemptsState]:
    attempt = next((row for row in state.attempts if _scope(row) == event.request.scope), None)
    # Retirement owns DISCARD; acquisition has no canonical disposal signal.
    if (
        attempt is None
        or attempt.phase != AttemptPhase.ACTIVE
        or _closed(attempt)
        or event.authority == RevisionAuthority.NONE
        or event.request.request_id is None
        or not isinstance(
            current_admission(attempt, event.request.scope, event.request.admission_id), Proven
        )
        or not isinstance(
            operation_for(
                context.run.receipts,
                RequestPrepared(
                    request=event.request, lifecycle=event.request.operation.schema_ref.lifecycle
                ),
            ),
            Proven,
        )
        or not any(
            descriptor.kind == event.request.operation.schema_ref.kind
            and descriptor.request_schema == event.request.operation.schema_ref.request_schema
            and descriptor.outcome_schema == event.request.operation.schema_ref.outcome_schema
            and descriptor.lifecycle == event.request.operation.schema_ref.lifecycle
            and descriptor.revision_authority == event.authority
            for descriptor in context.run.capabilities.operations
        )
        or (
            event.authority == RevisionAuthority.RESTORE
            and attempt.workspace.mode == WorkspaceMode.READ_ONLY_REVISION
        )
    ):
        return AreaChange(state=state)
    if any(
        row.scope == _scope(attempt)
        and row.turn.session.access == Access.WRITE_CANDIDATE
        and not _terminal(context, row, attempt)
        for row in context.sessions.invocations
    ):
        return AreaChange(state=state)
    if (
        event.request.request_id in attempt.pending_intents
        or _intent(context, event.request.request_id) is not None
    ):
        return AreaChange(state=state)
    # DISCARD releases the workspace hold, which only closure may do: Retirement dispatches a
    # declared discard operation as cleanup once the attempt is closing. An active attempt
    # declines it (the strategy must retire the attempt first).
    if event.authority == RevisionAuthority.DISCARD:
        return AreaChange(state=state)
    updated = attempt.model_copy(
        update={"pending_intents": (*attempt.pending_intents, event.request.request_id)}
    )
    return AreaChange(state=_replace(state, updated), requests=(event.request,))


type AcquisitionEvent = (
    WorkspaceObserved
    | AttemptReacquireRequested
    | InitialSessionsReady
    | InitialSessionsFailed
    | AttemptSetupFailed
)
type AccountingEvent = (
    InvocationChargeRequested
    | InvocationEnded
    | InvocationCheckpointRequested
    | InvocationCheckpointed
    | AttemptChargeRefundRequested
)


def advance(
    state: AttemptsState, context: AttemptsContext, event: AttemptsEvent
) -> AreaChange[AttemptsState]:
    """Apply typed acquisition events without mutating inputs or retirement fields."""
    if isinstance(event, AttemptEvaluationHistoryUpdated):
        scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
        owner = next(
            (
                row
                for row in state.attempts
                if row.attempt_id == scope.owner and row.generation == scope.generation
            ),
            None,
        )
        if owner is None or event.history != produce_history(
            scope, context.evaluation, context.intents, owner, context.run
        ):
            raise ContractError(("history",), "requires canonical measurement coverage")
        if any(record not in event.history.records for record in owner.evaluation_history.records):
            raise ContractError(("history",), "cannot replace durable terminal measurement facts")
        updated = owner.model_copy(update={"evaluation_history": event.history})
        return AreaChange(
            state=state.model_copy(
                update={
                    "attempts": tuple(updated if row == owner else row for row in state.attempts)
                }
            )
        )
    if isinstance(
        event,
        AttemptRegistered
        | AttemptAdmitted
        | RevisionOperationRequested
        | RevisionOperationObserved,
    ):
        return _advance_registration(state, context, event)
    if isinstance(
        event,
        WorkspaceObserved
        | AttemptReacquireRequested
        | InitialSessionsReady
        | InitialSessionsFailed
        | AttemptSetupFailed,
    ):
        attempt = _find(state, event.attempt)
        return (
            _advance_acquisition(state, context, attempt, event)
            if attempt is not None
            else AreaChange(state=state)
        )
    if isinstance(
        event,
        InvocationChargeRequested
        | InvocationEnded
        | InvocationCheckpointRequested
        | InvocationCheckpointed
        | AttemptChargeRefundRequested,
    ):
        attempt = _find(state, event.attempt)
        return (
            _advance_accounting(state, context, attempt, event)
            if attempt is not None
            else AreaChange(state=state)
        )
    raise ContractValidationError("event", "event belongs to another attempts leaf")


def _canonical_initial_specs(
    context: AttemptsContext, event: AttemptRegistered | AttemptAdmitted
) -> Verdict[DecisionReceipt]:
    rows = tuple(
        row for row in context.run.receipts if row.decision_id == event.request.decision_id
    )
    decision = rows[0].decision if len(rows) == 1 else None
    if not isinstance(decision, StartAttempt):
        return Missing(ProofReason.ABSENT_RECEIPT)
    canonical = decision.model_copy(
        update={
            "scope": Scope(owner=context.run.run_id, generation=context.run.generation),
            "attempt_id": event.request.attempt_id,
            "item_id": event.request.item_id,
            "workspace": event.workspace,
            "budget": event.budget,
            "initial_sessions": event.initial_sessions,
        }
    )
    if event.request.generation != decision.scope.generation:
        return Mismatch(ProofField.GENERATION)
    if event.request.admission_charge != event.budget.admission_charge:
        return Mismatch(ProofField.PAYLOAD)
    return accepted_receipt_for(context.run.receipts, event.request.decision_id, canonical)


def _registration_authority(
    state: AttemptsState, context: AttemptsContext, event: AttemptRegistered | AttemptAdmitted
) -> bool:
    proof = _canonical_initial_specs(context, event)
    if not isinstance(proof, Proven):
        return False
    existing = _find(
        state, AttemptRef(attempt_id=event.request.attempt_id, generation=event.request.generation)
    )
    return existing is not None or event.request.admission_charge <= admission_remaining(
        state.attempts, context.run.limits.max_attempts
    )


def _advance_registration(
    state: AttemptsState,
    context: AttemptsContext,
    event: AttemptRegistered
    | AttemptAdmitted
    | RevisionOperationRequested
    | RevisionOperationObserved,
) -> AreaChange[AttemptsState]:
    if isinstance(event, AttemptRegistered | AttemptAdmitted) and not _registration_authority(
        state, context, event
    ):
        return AreaChange(state=state)
    if isinstance(event, AttemptRegistered):
        attempt = _register(state, event)
        if _find(state, _ref(attempt)) is not None:
            return AreaChange(state=state)
        return AreaChange(state=state.model_copy(update={"attempts": (*state.attempts, attempt)}))
    if isinstance(event, AttemptAdmitted):
        return _admit(state, context, event)
    if isinstance(event, RevisionOperationRequested):
        return _revision_request(state, context, event)
    return _revision_observed(state, context, event)


def _revision_observed(
    state: AttemptsState, context: AttemptsContext, event: RevisionOperationObserved
) -> AreaChange[AttemptsState]:
    attempt = next((row for row in state.attempts if _scope(row) == event.observation.scope), None)
    intent = _intent(context, event.observation.request_id)
    if (
        attempt is None
        or intent is None
        or not isinstance(intent.request, ExecuteRegisteredOperation)
        or intent.request.operation_id != event.operation_id
        or intent.request.scope != _scope(attempt)
        or intent.request.admission_id != attempt.admission_id
        or not _fresh_observation(intent, event.observation)
        or not _current(attempt, event.observation)
        or event.observation.request_id not in attempt.pending_intents
    ):
        return AreaChange(state=state)
    if _unresolved_acceptance(event.observation):
        return _inspection(state, context, attempt, event.observation)
    if not event.observation.terminal:
        return AreaChange(state=state)
    updated = attempt.model_copy(
        update={
            "pending_intents": tuple(
                identity
                for identity in attempt.pending_intents
                if identity != event.observation.request_id
            )
        }
    )
    return AreaChange(state=_replace(state, updated))


def _advance_acquisition(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: AcquisitionEvent
) -> AreaChange[AttemptsState]:
    if isinstance(event, WorkspaceObserved):
        return _workspace(state, context, attempt, event)
    if isinstance(event, AttemptReacquireRequested):
        return _reacquire(state, context, attempt, event)
    if isinstance(event, InitialSessionsReady):
        group = next(
            (
                row
                for row in context.sessions.acquisition_groups
                if row.attempt == _ref(attempt) and row.admission_id == attempt.admission_id
            ),
            None,
        )
        if (
            event.admission_id != attempt.admission_id
            or group is None
            or group.phase != "ready"
            or event.session_ids != group.session_ids
        ):
            return AreaChange(state=state)
        return _ready(state, context, attempt)
    if isinstance(event, InitialSessionsFailed) and (
        event.admission_id != attempt.admission_id
        or not any(
            group.attempt == _ref(attempt)
            and group.admission_id == attempt.admission_id
            and event.session_id in group.session_ids
            for group in context.sessions.acquisition_groups
        )
        or (intent := _intent(context, event.observation.request_id)) is None
        or not isinstance(intent.request, EnsureSession)
        or intent.request.spec.session_id != event.session_id
    ):
        return AreaChange(state=state)
    return _setup_failed(state, context, attempt, event.observation, event.failure)


def _advance_accounting(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: AccountingEvent
) -> AreaChange[AttemptsState]:
    if isinstance(event, AttemptChargeRefundRequested):
        charge = next((row for row in attempt.charges if row.charge_id == event.charge_id), None)
        invocation = _receipt_invocation(context, attempt, charge) if charge is not None else None
    else:
        invocation = _invocation(context, attempt, event.invocation)
    observation = (
        (event.observation if isinstance(event, InvocationEnded) else invocation.observation)
        if invocation is not None
        else None
    )
    if (
        observation is not None
        and _current(attempt, observation)
        and _unresolved_acceptance(observation)
    ):
        return _inspection(state, context, attempt, observation)
    if isinstance(event, InvocationChargeRequested):
        return _charge(state, context, attempt, event)
    if isinstance(event, InvocationCheckpointRequested):
        return _checkpoint_request(state, context, attempt, event)
    if isinstance(event, InvocationCheckpointed):
        return _checkpointed(state, context, attempt, event)
    if isinstance(event, AttemptChargeRefundRequested):
        return _refund(state, context, attempt, event)
    return AreaChange(state=state)
