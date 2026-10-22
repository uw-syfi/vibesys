"""Pure acquisition, retained-checkpoint and receipt authority for attempts.

Retirement owns closure and release dependencies. This leaf only requests that
work through signals; canonical records retain acquisition and write proofs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._adoption import fences_root_mutation
from ._attempt_terminal import (
    attempt_ref,
    attempt_scope,
    exact_invocation_terminal,
    identity_mismatch,
    request_turn_matches,
    retained_acquisition,
    retained_sessions_for,
    successful,
    unresolved_acceptance,
)
from ._checkpoint_events import declined, retained
from ._deadlines import cleanup, from_dispatch, limit_to
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
from ._session_scope import retains_write_turn, write_turn_authority, yield_authority, yields_turn
from ._turn_holds import lost_without_debit
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
    CheckpointDecline,
    CheckpointDeclined,
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
    MAX_REASK_CHAIN,
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
    RequestId,
    RevisionAuthority,
    RunStatus,
    Scope,
    SetupFailureKind,
    WorkspaceMode,
    WorkspaceRef,
)
from .types.evaluation import ContinuationPhase
from .types.kernel import AreaChange, DecisionReceipt
from .types.records import (
    InspectRequest,
    Outbound,
    Request,
    RequestPhase,
    RequestPrepared,
    within,
)
from .types.scheduling import AttemptReady
from .types.sessions import (
    Access,
    EnsureSession,
    InvocationChargeRefunded,
    InvocationChargesAuthorized,
    InvocationCheckpointAvailable,
    InvocationCheckpointPending,
    SessionPhase,
    SessionsAcquireRequested,
)
from .types.strategy import Operation, RequestTurn, StartAttempt

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent
    from .types.evaluation import Continuation
    from .types.kernel import AttemptsContext
    from .types.records import RequestRecord
    from .types.sessions import InterruptClaim, Invocation, SessionView


def _identity(attempt: AttemptView, purpose: str) -> str:
    # Length prefixes keep opaque attempt/episode roots unambiguous.
    owner = attempt.attempt_id.root
    episode = f"id:{attempt.admission_id.root}" if attempt.admission_id is not None else "none"
    return f"attempt:{len(owner)}:{owner}:{attempt.generation}:{len(episode)}:{episode}:{len(purpose)}:{purpose}"


def _closed(attempt: AttemptView) -> bool:
    return isinstance(current_closure(attempt, attempt.closure), Proven)


def _find(state: AttemptsState, ref: AttemptRef) -> AttemptView | None:
    return next((attempt for attempt in state.attempts if attempt_ref(attempt) == ref), None)


def _replace(state: AttemptsState, attempt: AttemptView) -> AttemptsState:
    return state.model_copy(
        update={
            "attempts": tuple(
                attempt if attempt_ref(previous) == attempt_ref(attempt) else previous
                for previous in state.attempts
            )
        }
    )


def _within_run(context: AttemptsContext, request: Request) -> Outbound:
    """Workspace setup and restore may take the time the run has left."""
    return within(request, limit_to(context.run, context.run.deadline_at))


def _record(context: AttemptsContext, identity: RequestId) -> RequestRecord | None:
    return context.records.record(identity)


def _fresh_observation(record: RequestRecord, observation: Observation) -> bool:
    return (
        record.observation is None
        or observation.sequence > record.observation.sequence
        or observation == record.observation
    )


def _current(attempt: AttemptView, observation: Observation) -> bool:
    return isinstance(
        current_admission(attempt, observation.scope, observation.admission_id), Proven
    )


def _inspection(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, observation: Observation
) -> AreaChange[AttemptsState]:
    original = _record(context, observation.request_id)
    if (
        original is None
        or original.request.scope != attempt_scope(attempt)
        or original.request.admission_id != attempt.admission_id
        or not _fresh_observation(original, observation)
    ):
        return AreaChange(state=state)
    identity = RequestId(root=f"inspect:{observation.request_id.root}:{observation.sequence}")
    if _record(context, identity) is not None:
        return AreaChange(state=state)
    return AreaChange(
        state=state,
        requests=(
            within(
                InspectRequest(
                    request_id=identity,
                    scope=attempt_scope(attempt),
                    admission_id=attempt.admission_id,
                    target=observation.request_id,
                    # Root lease identity remains in the original observation.
                    resource_id=None,
                ),
                context.run.limits.reconciliation_bound,
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
            or parent.pending_records
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
    return fences_root_mutation(context.settlement, context.records) or any(
        attempt_ref(other) != attempt_ref(attempt)
        and other.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT
        and (
            other.phase not in (AttemptPhase.QUEUED, AttemptPhase.TERMINAL, AttemptPhase.PARKED)
            or other.release_dependencies
            or other.pending_records
        )
        for other in state.attempts
    )


def _registered(state: AttemptsState, attempt: AttemptView) -> AttemptsState:
    if _find(state, attempt_ref(attempt)) is not None:
        return state
    return state.model_copy(update={"attempts": (*state.attempts, attempt)})


def _decline_root(
    attempt: AttemptView, context: AttemptsContext, admission_id: DecisionId
) -> RetireRequested:
    return RetireRequested(
        attempt=attempt_ref(attempt),
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
            if _find(state, attempt_ref(attempt)) is not None
            else state.model_copy(update={"attempts": (*state.attempts, attempt)})
        )
        return AreaChange(
            state=registered,
            signals=(
                RetireRequested(
                    attempt=attempt_ref(attempt),
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
            "pending_records": (*attempt.pending_records, identity),
            "sessions": tuple(spec.session_id for spec in event.initial_sessions),
        }
    )
    registered = (
        state
        if _find(state, attempt_ref(attempt)) is not None
        else state.model_copy(update={"attempts": (*state.attempts, attempt)})
    )
    return AreaChange(
        state=_replace(registered, attempt),
        requests=(
            _within_run(
                context,
                EnsureWorkspace(
                    request_id=identity,
                    scope=attempt_scope(attempt),
                    admission_id=attempt.admission_id,
                    attempt=attempt_ref(attempt),
                    plan=attempt.workspace,
                ),
            ),
        ),
        signals=(
            SessionsAcquireRequested(
                attempt=attempt_ref(attempt),
                admission_id=event.admission_id,
                scope=attempt_scope(attempt),
                specs=event.initial_sessions,
            ),
        )
        if event.initial_sessions
        else (),
    )


def _workspace_request_matches(attempt: AttemptView, record: RequestRecord) -> bool:
    request = record.request
    if isinstance(request, EnsureWorkspace):
        payload_matches = request.plan == attempt.workspace
    elif isinstance(request, RestoreRevision):
        payload_matches = request.revision == attempt.checkpoint
    else:
        return False
    return (
        payload_matches
        and request.attempt == attempt_ref(attempt)
        and request.scope == attempt_scope(attempt)
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
            if group.attempt == attempt_ref(attempt) and group.admission_id == attempt.admission_id
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
        and row.request.scope == attempt_scope(attempt)
        and row.request.admission_id == attempt.admission_id
        and row.request_id in attempt.pending_records
        for row in context.records.records
    ):
        return AreaChange(state=state)
    # A retained canonical observation is the durable workspace readiness proof.
    proofs = tuple(
        row
        for row in context.records.records
        if isinstance(row.request, EnsureWorkspace | RestoreRevision)
        and row.request.scope == attempt_scope(attempt)
        and row.request.admission_id == attempt.admission_id
    )
    if not proofs or any(
        not _workspace_request_matches(attempt, row)
        or row.phase != RequestPhase.COMPLETED
        or row.observation is None
        or not _current(attempt, row.observation)
        or row.observation.request_id != row.request_id
        or not successful(row.observation)
        for row in proofs
    ):
        return AreaChange(state=state)
    if any(isinstance(row.request, RestoreRevision) for row in proofs):
        return _reacquisition_ready(state, context, attempt, proofs)
    active = attempt.model_copy(update={"phase": AttemptPhase.ACTIVE})
    return AreaChange(
        state=_replace(state, active),
        signals=(AttemptReady(attempt=attempt_ref(active), admission_id=attempt.admission_id),),
    )


def _reacquisition_ready(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    proofs: tuple[RequestRecord, ...],
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
        retained_acquisition(attempt, row, context.sessions, context.records)
        for row in retained.value
    ):
        return AreaChange(state=state)
    required = tuple(row.spec.session_id for row in retained.value)
    if not any(
        group.attempt == attempt_ref(attempt)
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
                attempt=attempt_ref(attempt),
                request_id=continuation.reopen_authority,
                admission_id=attempt.admission_id,
            ),
        ),
    )


def _workspace(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: WorkspaceObserved
) -> AreaChange[AttemptsState]:
    observation = event.observation
    record = _record(context, observation.request_id)
    if (
        not _current(attempt, observation)
        or record is None
        or record.request_id not in attempt.pending_records
        or not _fresh_observation(record, observation)
    ):
        return AreaChange(state=state)
    if (
        not isinstance(record.request, EnsureWorkspace | RestoreRevision)
        or not _workspace_request_matches(attempt, record)
        or record.request.scope != attempt_scope(attempt)
        or record.request.admission_id != attempt.admission_id
    ):
        return AreaChange(state=state)
    if unresolved_acceptance(observation):
        return _inspection(state, context, attempt, observation)
    if (
        observation.status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
        and observation.terminal
    ):
        return _setup_failed(state, context, attempt, observation, SetupFailureKind.TRANSIENT)
    expected = (
        record.request.plan.base
        if isinstance(record.request, EnsureWorkspace)
        else record.request.revision
    )
    if (
        _closed(attempt)
        or attempt.phase != AttemptPhase.ACQUIRING
        or not successful(observation)
        or record.phase != RequestPhase.COMPLETED
        or record.observation != observation
        or event.revision != expected
    ):
        return AreaChange(state=state)
    updated = attempt.model_copy(
        update={
            "pending_records": tuple(
                identity
                for identity in attempt.pending_records
                if identity != observation.request_id
            ),
            "base_input": observation.measured_input
            if isinstance(record.request, EnsureWorkspace)
            else attempt.base_input,
        }
    )
    state = _replace(state, updated)
    return _ready(state, context, updated)


def _setup_origin(context: AttemptsContext, attempt: AttemptView, observation: Observation) -> bool:
    record = _record(context, observation.request_id)
    if (
        record is None
        or record.request.scope != attempt_scope(attempt)
        or record.request.admission_id != attempt.admission_id
        or not _fresh_observation(record, observation)
    ):
        return False
    if isinstance(record.request, EnsureWorkspace | RestoreRevision):
        return (
            record.request_id in attempt.pending_records
            and record.request.attempt == attempt_ref(attempt)
        )
    if isinstance(record.request, EnsureSession):
        return any(
            group.attempt == attempt_ref(attempt)
            and group.admission_id == attempt.admission_id
            and group.failure_request == observation.request_id
            and group.phase == "failed"
            and record.request.spec.session_id in group.session_ids
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
    if unresolved_acceptance(observation) or (
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
        events=(AttemptExhausted(attempt=attempt_ref(attempt), reason="paid-limit"),)
        if exhausted
        else (),
        signals=(
            RetireRequested(
                attempt=attempt_ref(attempt),
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
    proof = invocation_for(context.sessions.invocations, ref, attempt_scope(attempt))
    return proof.value if isinstance(proof, Proven) else None


def _session_scope(context: AttemptsContext, attempt: AttemptView, session: SessionView) -> bool:
    return session.scope in (
        attempt_scope(attempt),
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
    proof = retained_sessions_for(attempt, continuation, context.sessions, context.records)
    if isinstance(proof, Proven) and not all(
        _session_scope(context, attempt, row) for row in proof.value
    ):
        return Mismatch(ProofField.SCOPE)
    return proof


def _terminal(context: AttemptsContext, invocation: Invocation, attempt: AttemptView) -> bool:
    session = _session(context, attempt, invocation.invocation)
    return _billing_origin(context, attempt, invocation) and isinstance(
        exact_invocation_terminal(invocation, session, context.records, attempt.admission_id),
        Proven,
    )


def _correction_allowed(
    context: AttemptsContext, attempt: AttemptView, invocation: Invocation
) -> bool:
    predecessor = invocation.turn.predecessor
    seen = {invocation.invocation}
    links = depth = 0
    child = invocation
    while predecessor is not None:
        if predecessor in seen:
            return False
        seen.add(predecessor)
        previous = _invocation(context, attempt, predecessor)
        if previous is None or not _terminal(context, previous, attempt):
            return False
        if _completed_interruption(context, attempt, child) is not True:
            links += 1
            # A re-ask after a turn lost to its provider is no link: the gate bounds an outage.
            depth += 0 if lost_without_debit(context.records, previous) else 1
        child = previous
        predecessor = previous.turn.predecessor if previous.turn.charge_class != "resume" else None
    return links > 0 and depth <= MAX_REASK_CHAIN


def _initial_session_acquisition(
    context: AttemptsContext, session: SessionView, ref: InvocationRef
) -> bool:
    # Sessions publishes acquisition IDs before quiescent outbox registration.
    return (
        session.phase == SessionPhase.ACQUIRING
        and session.invocation == ref.invocation_id
        and bool(session.pending_records)
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
        and row.decision.scope == attempt_scope(attempt)
        and isinstance(
            accepted_receipt_for(context.run.receipts, row.decision_id, row.decision), Proven
        )
    )
    return len(origins) == 1 and (
        (invocation.phase == SessionPhase.ACQUIRING and invocation.observation is None)
        or any(
            request_turn_matches(invocation, row.request)
            and (
                not isinstance(row.request, ExecuteRegisteredOperation)
                or isinstance(operation_for(context.run.receipts, row.request), Proven)
            )
            and row.request.scope == attempt_scope(attempt)
            and row.request.admission_id == attempt.admission_id
            and row.request.decision_id == origins[0].decision_id
            and row.request_id in origins[0].request_ids
            for row in context.records.records
        )
    )


def _read_only_view(attempt: AttemptView, invocation: Invocation) -> bool:
    """A read-only look at a revision the attempt retained exceeds nothing its workspace allows.

    A reviewer of an isolated candidate reads the retained revision under the
    read-only mode that settlement requires to attribute its verdict. The view
    must name one of this attempt's own retained revisions: a read-only view of
    any other revision is not work the attempt can be charged for.
    """
    workspace = invocation.turn.workspace
    return (
        isinstance(workspace, WorkspaceRef)
        and workspace.mode == WorkspaceMode.READ_ONLY_REVISION
        and invocation.turn.session.access == Access.READ_ONLY
        and any(checkpoint.revision == workspace.revision for checkpoint in attempt.checkpoints)
    )


def _chargeable_invocation(
    context: AttemptsContext, attempt: AttemptView, ref: InvocationRef
) -> Invocation | None:
    invocation = _invocation(context, attempt, ref)
    if (
        attempt.phase != AttemptPhase.ACTIVE
        or not isinstance(
            current_admission(attempt, attempt_scope(attempt), attempt.admission_id), Proven
        )
        or _closed(attempt)
        or invocation is None
        or invocation.phase in (SessionPhase.UNKNOWN, SessionPhase.CLOSING, SessionPhase.TERMINAL)
        or (
            invocation.observation is not None
            and (
                not _current(attempt, invocation.observation)
                or unresolved_acceptance(invocation.observation)
                or not isinstance(
                    observation_for(
                        _record(context, invocation.observation.request_id), invocation.observation
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
        != attempt_scope(attempt)
        or (
            isinstance(invocation.turn.workspace, WorkspaceRef)
            and invocation.turn.workspace.mode != attempt.workspace.mode
            and not _read_only_view(attempt, invocation)
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
            state=state,
            events=(AttemptExhausted(attempt=attempt_ref(attempt), reason="retry-limit"),),
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
            state=state,
            events=(AttemptExhausted(attempt=attempt_ref(attempt), reason="paid-limit"),),
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


def _checkpoint_authorized(
    context: AttemptsContext, invocation: Invocation, identity: RequestId
) -> bool:
    """An interruption claim, the conclusive write turn's own derived authority, or a yield's.

    A turn that yielded is checkpointed under its yield authority, whatever its access,
    because the checkpoint is what publishes its suspension.
    """
    return (
        _interrupt_checkpoint(context, invocation.invocation, identity)
        or (
            retains_write_turn(invocation)
            and identity == write_turn_authority(invocation.invocation)
        )
        or (yields_turn(invocation) and identity == yield_authority(invocation.invocation))
    )


def _checkpoint_decline(
    context: AttemptsContext, attempt: AttemptView, invocation: Invocation, identity: RequestId
) -> CheckpointDecline | None:
    """The reason this terminal invocation takes no checkpoint now, or None to take it."""
    if not _checkpoint_authorized(context, invocation, identity):
        return CheckpointDecline.UNAUTHORIZED
    if not any(
        charge.invocation_id == invocation.invocation.invocation_id
        and charge.historical_proof is None
        for charge in attempt.charges
    ):
        return CheckpointDecline.UNCHARGED
    if identity == write_turn_authority(invocation.invocation) and (
        attempt.phase != AttemptPhase.ACTIVE or attempt.closure is not None
    ):
        # Dispatch of an ordinary mutation needs a live episode; closure retains instead.
        return CheckpointDecline.NOT_ACTIVE
    if any(
        row.scope == attempt_scope(attempt)
        and row.invocation != invocation.invocation
        and row.turn.session.access == Access.WRITE_CANDIDATE
        and not _terminal(context, row, attempt)
        for row in context.sessions.invocations
    ):
        return CheckpointDecline.WRITER_ACTIVE
    return None


def _decline_checkpoint(
    state: AttemptsState,
    attempt: AttemptView,
    event: InvocationCheckpointRequested,
    reason: CheckpointDecline,
) -> AreaChange[AttemptsState]:
    """Record why no checkpoint is requested; never silent, never repeated."""
    row = CheckpointDeclined(invocation=event.invocation, authority=event.authority, reason=reason)
    if row in attempt.checkpoint_declines:
        return AreaChange(state=state)
    updated = attempt.model_copy(
        update={"checkpoint_declines": (*attempt.checkpoint_declines, row)}
    )
    return AreaChange(state=_replace(state, updated), events=declined(event.invocation, reason))


def _resume_deferred_checkpoint(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: InvocationEnded,
) -> AreaChange[AttemptsState]:
    """Keep the promise of a ``WRITER_ACTIVE`` decline when the later writer ends empty-handed.

    A conclusive write turn that ends while another writer of the workspace is still running
    is declined, because a snapshot would be torn; the decline leaves the checkpoint to the
    later writer's own terminal turn. That turn earns one only when it succeeds. When it
    fails (crash, timeout, bad reply, cancellation) it requests nothing, and the earlier
    writer's work would stay unretained: a search that keeps its revision then has the
    closure's retention rejected, and the attempt never releases. So every end of a writer
    that earns no checkpoint of its own asks again for the last deferred one; if another
    writer is still running, the ask is declined again, silently, and the next end repeats it.
    """
    ended = _invocation(context, attempt, event.invocation)
    if (
        ended is None
        or ended.turn.session.access != Access.WRITE_CANDIDATE
        or retains_write_turn(ended)
    ):
        return AreaChange(state=state)
    deferred = [
        row
        for row in attempt.checkpoint_declines
        if row.reason == CheckpointDecline.WRITER_ACTIVE
        and row.authority == write_turn_authority(row.invocation)
        and row.authority not in attempt.pending_records
        and all(checkpoint.request_id != row.authority for checkpoint in attempt.checkpoints)
    ]
    if not deferred:
        return AreaChange(state=state)
    last = deferred[-1]
    return AreaChange(
        state=state,
        signals=(
            InvocationCheckpointRequested(
                attempt=attempt_ref(attempt),
                invocation=last.invocation,
                retention="wip",
                authority=last.authority,
            ),
        ),
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
    identity = event.authority
    pending = InvocationCheckpointPending(invocation=event.invocation, request_id=identity)
    if any(checkpoint.request_id == identity for checkpoint in attempt.checkpoints):
        return AreaChange(state=state)
    if identity in attempt.pending_records:
        # The request is already out; whatever newly waits on this checkpoint waits on it.
        return AreaChange(state=state, signals=(pending,))
    declined = _checkpoint_decline(context, attempt, invocation, identity)
    if declined is not None:
        return _decline_checkpoint(state, attempt, event, declined)
    request = SnapshotAndRetain(
        request_id=identity,
        scope=attempt_scope(attempt),
        admission_id=attempt.admission_id,
        attempt=attempt_ref(attempt),
        retention=event.retention,
        invocation=event.invocation,
    )
    updated = attempt.model_copy(update={"pending_records": (*attempt.pending_records, identity)})
    # The retain is how the attempt closes. The time the run has left is zero once the
    # deadline has passed, and a turn that ends during a publication is committed then:
    # cleanup's own budget is what lets the snapshot still run.
    return AreaChange(
        state=_replace(state, updated),
        requests=(cleanup(context.run, request),),
        signals=(pending,),
    )


def _checkpoint_acknowledged(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: WorkspaceObserved,
) -> AreaChange[AttemptsState]:
    """Turn the workspace's answer to an invocation snapshot into the attempt's checkpoint.

    The ledger forwards every answer as a WorkspaceObserved, and the executor cannot
    know the attempt's charges, so core completes the proof from its own records and
    runs it through the same validation as an explicit InvocationCheckpointed.
    """
    observation = event.observation
    record = _record(context, observation.request_id)
    request = record.request if record is not None else None
    if (
        not isinstance(request, SnapshotAndRetain)
        or request.invocation is None
        or request.attempt != attempt_ref(attempt)
        or observation.request_id not in attempt.pending_records
        or not _current(attempt, observation)
        or not observation.terminal
    ):
        return AreaChange(state=state)
    charge = next(
        (
            row
            for row in attempt.charges
            if row.invocation_id == request.invocation.invocation_id
            and row.historical_proof is None
        ),
        None,
    )
    if observation.status == ObservationStatus.SUCCEEDED and event.revision is not None:
        if charge is None:
            return AreaChange(state=state)
        return _checkpointed(
            state,
            context,
            attempt,
            InvocationCheckpointed(
                attempt=attempt_ref(attempt),
                revision=event.revision,
                charge=charge,
                invocation=request.invocation,
                checkpoint_request=observation.request_id,
            ),
        )
    # A conclusive failure retains nothing here; release must not wait on it forever.
    failed = CheckpointDecline.SNAPSHOT_FAILED
    updated = attempt.model_copy(
        update={
            "pending_records": tuple(
                rid for rid in attempt.pending_records if rid != observation.request_id
            ),
            "checkpoint_declines": (
                *attempt.checkpoint_declines,
                CheckpointDeclined(
                    invocation=request.invocation, authority=observation.request_id, reason=failed
                ),
            ),
        }
    )
    return AreaChange(state=_replace(state, updated), events=declined(request.invocation, failed))


def _checkpointed(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: InvocationCheckpointed,
) -> AreaChange[AttemptsState]:
    if any(row.request_id == event.checkpoint_request for row in attempt.checkpoints):
        return AreaChange(state=state)
    invocation = _invocation(context, attempt, event.invocation)
    record = _record(context, event.checkpoint_request)
    if (
        invocation is None
        or not _terminal(context, invocation, attempt)
        or event.charge not in attempt.charges
        or event.charge.invocation_id != event.invocation.invocation_id
        or event.charge.historical_proof is not None
        or not _checkpoint_authorized(context, invocation, event.checkpoint_request)
    ):
        return AreaChange(state=state)
    if (
        record is None
        or not isinstance(record.request, SnapshotAndRetain)
        or record.request.scope != attempt_scope(attempt)
        or record.request.attempt != attempt_ref(attempt)
        or record.request.retention != "wip"
        or record.request.invocation != event.invocation
        or record.request.admission_id != attempt.admission_id
        or record.request_id not in attempt.pending_records
        or record.phase != RequestPhase.COMPLETED
        or record.observation is None
        or record.observation.request_id != event.checkpoint_request
        or not _current(attempt, record.observation)
        or not successful(record.observation)
        or record.observation.revision != event.revision
    ):
        return AreaChange(state=state)
    checkpoint = AttemptCheckpoint(
        invocation=event.invocation,
        request_id=event.checkpoint_request,
        revision=event.revision,
        retention=record.request.retention,
        measured_input=record.observation.measured_input,
    )
    updated = attempt.model_copy(
        update={
            "checkpoints": (*attempt.checkpoints, checkpoint),
            "pending_records": tuple(
                identity
                for identity in attempt.pending_records
                if identity != event.checkpoint_request
            ),
        }
    )
    return AreaChange(
        state=_replace(state, updated),
        events=retained(event.invocation, checkpoint.revision),
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
    mismatch = identity_mismatch(
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
            state=state,
            events=(AttemptExhausted(attempt=attempt_ref(attempt), reason="refund-limit"),),
        )
    if (
        charge.kind == ChargeKind.ATTEMPT
        and sum(row.refunded for row in attempt.charges if row.kind == ChargeKind.ATTEMPT)
        + event.amount
        > attempt.budget.refund_limit
    ):
        return AreaChange(
            state=state,
            events=(AttemptExhausted(attempt=attempt_ref(attempt), reason="refund-limit"),),
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
        or not isinstance(
            current_admission(attempt, attempt_scope(attempt), event.admission_id), Proven
        )
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
    if identity in attempt.pending_records or _record(context, identity) is not None:
        return AreaChange(state=state)
    updated = attempt.model_copy(update={"pending_records": (*attempt.pending_records, identity)})
    return AreaChange(
        state=_replace(state, updated),
        requests=(
            _within_run(
                context,
                RestoreRevision(
                    request_id=identity,
                    scope=attempt_scope(attempt),
                    admission_id=attempt.admission_id,
                    attempt=attempt_ref(attempt),
                    revision=event.base,
                ),
            ),
        ),
        signals=(
            SessionsAcquireRequested(
                attempt=attempt_ref(attempt),
                admission_id=event.admission_id,
                scope=attempt_scope(attempt),
                specs=tuple(row.spec for row in sessions),
            ),
        )
        if sessions
        else (),
    )


def _revision_request(
    state: AttemptsState, context: AttemptsContext, event: RevisionOperationRequested
) -> AreaChange[AttemptsState]:
    attempt = next(
        (row for row in state.attempts if attempt_scope(row) == event.request.scope), None
    )
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
                    request=event.request,
                    until=event.until,
                    lifecycle=event.request.operation.schema_ref.lifecycle,
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
        row.scope == attempt_scope(attempt)
        and row.turn.session.access == Access.WRITE_CANDIDATE
        and not _terminal(context, row, attempt)
        for row in context.sessions.invocations
    ):
        return AreaChange(state=state)
    if (
        event.request.request_id in attempt.pending_records
        or _record(context, event.request.request_id) is not None
    ):
        return AreaChange(state=state)
    # DISCARD releases the workspace hold, which only closure may do: Retirement dispatches a
    # declared discard operation as cleanup once the attempt is closing. An active attempt
    # declines it (the strategy must retire the attempt first).
    if event.authority == RevisionAuthority.DISCARD:
        return AreaChange(state=state)
    updated = attempt.model_copy(
        update={"pending_records": (*attempt.pending_records, event.request.request_id)}
    )
    return AreaChange(
        state=_replace(state, updated),
        requests=(Outbound(request=event.request, until=from_dispatch(context.run, event.until)),),
    )


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
            scope, context.evaluation, context.records, owner, context.run
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
        if _find(state, attempt_ref(attempt)) is not None:
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
    attempt = next(
        (row for row in state.attempts if attempt_scope(row) == event.observation.scope), None
    )
    record = _record(context, event.observation.request_id)
    if (
        attempt is None
        or record is None
        or not isinstance(record.request, ExecuteRegisteredOperation)
        or record.request.operation_id != event.operation_id
        or record.request.scope != attempt_scope(attempt)
        or record.request.admission_id != attempt.admission_id
        or not _fresh_observation(record, event.observation)
        or not _current(attempt, event.observation)
        or event.observation.request_id not in attempt.pending_records
    ):
        return AreaChange(state=state)
    if unresolved_acceptance(event.observation):
        return _inspection(state, context, attempt, event.observation)
    if not event.observation.terminal:
        return AreaChange(state=state)
    updated = attempt.model_copy(
        update={
            "pending_records": tuple(
                identity
                for identity in attempt.pending_records
                if identity != event.observation.request_id
            )
        }
    )
    return AreaChange(state=_replace(state, updated))


def is_invocation_snapshot(context: AttemptsContext, event: WorkspaceObserved) -> bool:
    """Whether a workspace observation answers an invocation's checkpoint request."""
    record = _record(context, event.observation.request_id)
    return (
        record is not None
        and isinstance(record.request, SnapshotAndRetain)
        and record.request.invocation is not None
    )


def _workspace_answer(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: WorkspaceObserved
) -> AreaChange[AttemptsState]:
    if is_invocation_snapshot(context, event):
        return _checkpoint_acknowledged(state, context, attempt, event)
    return _workspace(state, context, attempt, event)


def _advance_acquisition(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: AcquisitionEvent
) -> AreaChange[AttemptsState]:
    if isinstance(event, WorkspaceObserved):
        return _workspace_answer(state, context, attempt, event)
    if isinstance(event, AttemptReacquireRequested):
        return _reacquire(state, context, attempt, event)
    if isinstance(event, InitialSessionsReady):
        group = next(
            (
                row
                for row in context.sessions.acquisition_groups
                if row.attempt == attempt_ref(attempt) and row.admission_id == attempt.admission_id
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
            group.attempt == attempt_ref(attempt)
            and group.admission_id == attempt.admission_id
            and event.session_id in group.session_ids
            for group in context.sessions.acquisition_groups
        )
        or (record := _record(context, event.observation.request_id)) is None
        or not isinstance(record.request, EnsureSession)
        or record.request.spec.session_id != event.session_id
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
        and unresolved_acceptance(observation)
    ):
        return _inspection(state, context, attempt, observation)
    if isinstance(event, InvocationChargeRequested):
        return _charge(state, context, attempt, event)
    if isinstance(event, AttemptChargeRefundRequested):
        return _refund(state, context, attempt, event)
    return _advance_checkpoint(state, context, attempt, event)


def _advance_checkpoint(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: InvocationEnded | InvocationCheckpointRequested | InvocationCheckpointed,
) -> AreaChange[AttemptsState]:
    if isinstance(event, InvocationEnded):
        return _resume_deferred_checkpoint(state, context, attempt, event)
    if isinstance(event, InvocationCheckpointRequested):
        return _checkpoint_request(state, context, attempt, event)
    return _checkpointed(state, context, attempt, event)
