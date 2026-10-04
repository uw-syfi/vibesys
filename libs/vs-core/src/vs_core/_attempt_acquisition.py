"""Pure acquisition, retained-checkpoint and receipt authority for attempts.

Retirement owns closure and release dependencies. This leaf only requests that
work through signals; canonical intents retain acquisition and write proofs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import (
    AttemptAdmitted,
    AttemptChargeRefundRequested,
    AttemptCheckpoint,
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
    Observation,
    ObservationStatus,
    RequestId,
    RevisionAuthority,
    Scope,
    SetupFailureKind,
    WorkspaceMode,
    WorkspaceRef,
)
from .types.evaluation import ContinuationPhase
from .types.intents import InspectRequest, IntentPhase
from .types.kernel import AreaChange
from .types.scheduling import AttemptReady
from .types.sessions import (
    Access,
    EnsureSession,
    InvocationChargeRefunded,
    InvocationChargesAuthorized,
    InvocationCheckpointAvailable,
    SessionPhase,
    SessionsAcquireRequested,
)

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent
    from .types.common import InvocationRef
    from .types.intents import Intent
    from .types.kernel import AttemptsContext
    from .types.sessions import Invocation, SessionView


def _ref(attempt: AttemptView) -> AttemptRef:
    return AttemptRef(attempt_id=attempt.attempt_id, generation=attempt.generation)


def _scope(attempt: AttemptView) -> Scope:
    return Scope(owner=attempt.attempt_id, generation=attempt.generation)


def _identity(attempt: AttemptView, purpose: str) -> str:
    episode = attempt.admission_id.root if attempt.admission_id is not None else "queued"
    return f"attempt:{attempt.attempt_id.root}:{attempt.generation}:{episode}:{purpose}"


def _closed(attempt: AttemptView) -> bool:
    return attempt.closure is not None and attempt.closure.admission_id == attempt.admission_id


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
    return (
        observation.scope == _scope(attempt)
        and observation.admission_id == attempt.admission_id
        and attempt.admission_id is not None
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
                resource_id=observation.resource_id,
            ),
        ),
    )


def _register(state: AttemptsState, event: AttemptRegistered | AttemptAdmitted) -> AttemptView:
    ref = AttemptRef(attempt_id=event.request.attempt_id, generation=event.request.generation)
    previous = _find(state, ref)
    if previous is not None:
        if (
            previous.item_id != event.request.item_id
            or previous.workspace != event.workspace
            or previous.budget != event.budget
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
        charges=(
            ChargeReceipt(
                charge_id=ChargeId(root=f"admission:{event.request.decision_id.root}"),
                kind=ChargeKind.ADMISSION,
                charged=event.request.admission_charge,
            ),
        ),
    )


def _admit(
    state: AttemptsState, context: AttemptsContext, event: AttemptAdmitted
) -> AreaChange[AttemptsState]:
    attempt = _register(state, event)
    if attempt.phase != AttemptPhase.QUEUED or _closed(attempt):
        return AreaChange(state=state)
    if event.admission_id != event.request.decision_id:
        raise ContractValidationError("admission_id", "initial admission must match registration")
    if attempt.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT and any(
        other.attempt_id != attempt.attempt_id
        and other.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT
        and other.phase not in (AttemptPhase.QUEUED, AttemptPhase.TERMINAL, AttemptPhase.PARKED)
        for other in state.attempts
    ):
        raise ContractValidationError("workspace", "exclusive root is already owned")
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


def _ready(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView
) -> AreaChange[AttemptsState]:
    if attempt.phase != AttemptPhase.ACQUIRING or attempt.admission_id is None:
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
        group is None or group.phase != "ready" or group.session_ids != attempt.sessions
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
        row.phase != IntentPhase.COMPLETED
        or row.observation is None
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
        or intent.request.scope != _scope(attempt)
        or intent.request.admission_id != attempt.admission_id
    ):
        return AreaChange(state=state)
    if observation.status == ObservationStatus.UNKNOWN:
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
        return intent.request.spec.session_id in attempt.sessions and any(
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
    if failure == SetupFailureKind.UNKNOWN or observation.status == ObservationStatus.UNKNOWN:
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
    # Receipts lack an admission episode. An old paid receipt without a
    # correlated invocation observation cannot be separated from this setup
    # cycle after reopen, so do not invent another setup debit from ambiguity.
    paid = any(
        charge.kind == ChargeKind.ATTEMPT and charge.invocation_id is not None
        for charge in attempt.charges
    )
    charges = (
        attempt.charges
        if paid
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
    if ref.generation != attempt.generation:
        return None
    return next(
        (
            row
            for row in context.sessions.invocations
            if row.invocation == ref and row.scope == _scope(attempt)
        ),
        None,
    )


def _terminal(invocation: Invocation, attempt: AttemptView) -> bool:
    observation = invocation.observation
    return (
        observation is not None
        and _current(attempt, observation)
        and observation.terminal
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def _correction_allowed(
    context: AttemptsContext, attempt: AttemptView, invocation: Invocation
) -> bool:
    predecessor = invocation.turn.predecessor
    seen = {invocation.invocation}
    depth = 0
    while predecessor is not None:
        if predecessor in seen:
            return False
        seen.add(predecessor)
        previous = _invocation(context, attempt, predecessor)
        if previous is None or not _terminal(previous, attempt):
            return False
        depth += 1
        predecessor = (
            previous.turn.predecessor
            if previous.turn.charge_class in ("paid", "correction")
            else None
        )
    return depth > 0 and depth <= min(attempt.budget.retry_limit, context.run.limits.max_retries)


def _reattached(context: AttemptsContext, attempt: AttemptView, session: SessionView) -> bool:
    if session.resource_id is None:
        return False
    group_ready = any(
        group.attempt == _ref(attempt)
        and group.admission_id == attempt.admission_id
        and session.spec.session_id in group.session_ids
        and group.phase == "ready"
        for group in context.sessions.acquisition_groups
    )
    intent_ready = any(
        isinstance(row.request, EnsureSession)
        and row.request.spec == session.spec
        and row.request.required_resource == session.resource_id
        and row.request.scope == session.scope
        and row.request.admission_id == attempt.admission_id
        and row.phase == IntentPhase.COMPLETED
        and row.observation is not None
        and _successful(row.observation)
        and row.observation.resource_id == session.resource_id
        for row in context.intents.intents
    )
    return group_ready or intent_ready


def _chargeable_invocation(
    context: AttemptsContext, attempt: AttemptView, ref: InvocationRef
) -> Invocation | None:
    invocation = _invocation(context, attempt, ref)
    if (
        attempt.phase != AttemptPhase.ACTIVE
        or _closed(attempt)
        or invocation is None
        or invocation.turn.invocation_id != ref.invocation_id
        or invocation.turn.session.session_id != ref.session_id
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
    session = next(
        (
            row
            for row in context.sessions.sessions
            if row.spec.session_id == ref.session_id
            and row.scope
            in (_scope(attempt), Scope(owner=context.run.run_id, generation=context.run.generation))
            and row.generation == ref.generation
        ),
        None,
    )
    if (
        session is None
        or session.spec != invocation.turn.session
        or session.phase not in (SessionPhase.IDLE, SessionPhase.EXECUTING)
        or session.invocation not in (None, ref.invocation_id)
        or (session.spec.policy == "reuse" and not _reattached(context, attempt, session))
    ):
        return None
    return invocation


def _charge(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: InvocationChargeRequested,
) -> AreaChange[AttemptsState]:
    invocation = _chargeable_invocation(context, attempt, event.invocation)
    if invocation is None:
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
        charge_class == "correction" or invocation.turn.predecessor is not None
    ) and not _correction_allowed(context, attempt, invocation):
        return AreaChange(
            state=state, events=(AttemptExhausted(attempt=_ref(attempt), reason="retry-limit"),)
        )
    if charge_class == "resume" and not any(
        continuation.continuation_id == invocation.turn.continuation_id
        and continuation.next_invocation == event.invocation
        and continuation.phase == ContinuationPhase.AUTHORIZED
        for continuation in context.evaluation.continuations
    ):
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
    return any(
        claim.invocation == invocation
        and claim.checkpoint_authority == identity
        and claim.phase in ("draining", "checkpointed")
        for claim in context.sessions.interrupts
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
        or not _terminal(invocation, attempt)
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
    # SnapshotAndRetain has no invocation field. Only an existing interruption
    # claim durably binds the supplied request identity to this invocation;
    # general/candidate attribution needs a frozen-contract extension.
    if event.retention != "wip" or not _interrupt_checkpoint(context, event.invocation, identity):
        return AreaChange(state=state)
    if (
        any(checkpoint.request_id == identity for checkpoint in attempt.checkpoints)
        or identity in attempt.pending_intents
    ):
        return AreaChange(state=state)
    if any(
        row.scope == _scope(attempt)
        and row.invocation != event.invocation
        and row.turn.session.access == Access.WRITE_CANDIDATE
        and not _terminal(row, attempt)
        for row in context.sessions.invocations
    ):
        return AreaChange(state=state)
    request = SnapshotAndRetain(
        request_id=identity,
        scope=_scope(attempt),
        admission_id=attempt.admission_id,
        deadline_at=context.run.deadline_at,
        attempt=_ref(attempt),
        retention=event.retention,
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
        or not _terminal(invocation, attempt)
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
        or intent.request.admission_id != attempt.admission_id
        or intent.request_id not in attempt.pending_intents
        or intent.phase != IntentPhase.COMPLETED
        or intent.observation is None
        or not _current(attempt, intent.observation)
        or not _successful(intent.observation)
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


def _refund_proof(
    context: AttemptsContext,
    attempt: AttemptView,
    charge: ChargeReceipt,
    event: AttemptChargeRefundRequested,
) -> Invocation | None:
    if charge.invocation_id is None:
        return None
    invocation = next(
        (
            row
            for row in context.sessions.invocations
            if row.invocation.invocation_id == charge.invocation_id and row.scope == _scope(attempt)
        ),
        None,
    )
    if invocation is None or not _terminal(invocation, attempt):
        return None
    if event.reason != "interrupted":
        return None
    claim = next(
        (
            row
            for row in context.sessions.interrupts
            if row.invocation == invocation.invocation and row.authority == event.authority
        ),
        None,
    )
    if (
        claim is None
        or claim.phase != "checkpointed"
        or claim.refund != event.amount
        or claim.checkpoint_authority != event.checkpoint_authority
        or claim.refunded_charge is not None
    ):
        return None
    if not any(
        row.invocation == invocation.invocation
        and row.request_id == event.checkpoint_authority
        and row.retention == "wip"
        for row in attempt.checkpoints
    ):
        return None
    return invocation


def _refund(
    state: AttemptsState,
    context: AttemptsContext,
    attempt: AttemptView,
    event: AttemptChargeRefundRequested,
) -> AreaChange[AttemptsState]:
    charge = next((row for row in attempt.charges if row.charge_id == event.charge_id), None)
    # REJECTED proves nonacceptance, not unsupported capability. SetupFailureKind
    # is not persisted by the frozen contract, so unsupported credit cannot be
    # authorized after reload, even when the resources are positively drained.
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
    refunded = sum(row.refunded for owner in state.attempts for row in owner.charges) + sum(
        row.refunded for row in context.sessions.run_charges
    )
    if refunded + event.amount > context.run.limits.max_refunds:
        return AreaChange(
            state=state, events=(AttemptExhausted(attempt=_ref(attempt), reason="refund-limit"),)
        )
    invocation = _refund_proof(context, attempt, charge, event)
    if charge.kind != ChargeKind.ATTEMPT or invocation is None:
        return AreaChange(state=state)
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
    ):
        return AreaChange(state=state)
    sessions = tuple(
        row
        for row in context.sessions.sessions
        if row.spec.session_id in attempt.sessions and row.scope == _scope(attempt)
    )
    if len(sessions) != len(attempt.sessions) or any(
        row.resource_id is None or row.spec.policy != "reuse" for row in sessions
    ):
        return AreaChange(state=state)
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
    if (
        attempt is None
        or attempt.phase != AttemptPhase.ACTIVE
        or _closed(attempt)
        or event.authority in (RevisionAuthority.NONE, RevisionAuthority.DISCARD)
        or event.request.request_id is None
    ):
        return AreaChange(state=state)
    if any(
        row.scope == _scope(attempt)
        and row.turn.session.access == Access.WRITE_CANDIDATE
        and not _terminal(row, attempt)
        for row in context.sessions.invocations
    ):
        return AreaChange(state=state)
    if (
        event.request.request_id in attempt.pending_intents
        or _intent(context, event.request.request_id) is not None
    ):
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


def _advance_registration(
    state: AttemptsState,
    context: AttemptsContext,
    event: AttemptRegistered
    | AttemptAdmitted
    | RevisionOperationRequested
    | RevisionOperationObserved,
) -> AreaChange[AttemptsState]:
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
        or not _current(attempt, event.observation)
        or event.observation.request_id not in attempt.pending_intents
    ):
        return AreaChange(state=state)
    if event.observation.status == ObservationStatus.UNKNOWN:
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


def _failed_member(context: AttemptsContext, event: InitialSessionsFailed) -> bool:
    intent = _intent(context, event.observation.request_id)
    return (
        intent is not None
        and isinstance(intent.request, EnsureSession)
        and intent.request.spec.session_id == event.session_id
    )


def _advance_acquisition(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: AcquisitionEvent
) -> AreaChange[AttemptsState]:
    if isinstance(event, WorkspaceObserved):
        return _workspace(state, context, attempt, event)
    if isinstance(event, AttemptReacquireRequested):
        return _reacquire(state, context, attempt, event)
    if isinstance(event, InitialSessionsReady):
        if event.admission_id != attempt.admission_id or event.session_ids != attempt.sessions:
            return AreaChange(state=state)
        return _ready(state, context, attempt)
    if isinstance(event, InitialSessionsFailed) and (
        event.admission_id != attempt.admission_id
        or event.session_id not in attempt.sessions
        or not _failed_member(context, event)
    ):
        return AreaChange(state=state)
    return _setup_failed(state, context, attempt, event.observation, event.failure)


def _invocation_ended(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: InvocationEnded
) -> AreaChange[AttemptsState]:
    if (
        _invocation(context, attempt, event.invocation) is not None
        and _current(attempt, event.observation)
        and event.observation.status == ObservationStatus.UNKNOWN
    ):
        return _inspection(state, context, attempt, event.observation)
    return AreaChange(state=state)


def _unknown_invocation(
    context: AttemptsContext, attempt: AttemptView, event: AccountingEvent
) -> Observation | None:
    if isinstance(event, AttemptChargeRefundRequested):
        charge = next((row for row in attempt.charges if row.charge_id == event.charge_id), None)
        invocation = next(
            (
                row
                for row in context.sessions.invocations
                if charge is not None
                and row.invocation.invocation_id == charge.invocation_id
                and row.scope == _scope(attempt)
            ),
            None,
        )
    else:
        invocation = _invocation(context, attempt, event.invocation)
    observation = invocation.observation if invocation is not None else None
    return (
        observation
        if observation is not None
        and _current(attempt, observation)
        and observation.status == ObservationStatus.UNKNOWN
        else None
    )


def _advance_accounting(
    state: AttemptsState, context: AttemptsContext, attempt: AttemptView, event: AccountingEvent
) -> AreaChange[AttemptsState]:
    unknown = _unknown_invocation(context, attempt, event)
    if unknown is not None:
        return _inspection(state, context, attempt, unknown)
    if isinstance(event, InvocationChargeRequested):
        return _charge(state, context, attempt, event)
    if isinstance(event, InvocationCheckpointRequested):
        return _checkpoint_request(state, context, attempt, event)
    if isinstance(event, InvocationCheckpointed):
        return _checkpointed(state, context, attempt, event)
    if isinstance(event, AttemptChargeRefundRequested):
        return _refund(state, context, attempt, event)
    return _invocation_ended(state, context, attempt, event)
