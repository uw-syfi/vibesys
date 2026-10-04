"""Durable attempt closure, release dependencies and guarded scope reopening."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import (
    AttemptClosure,
    AttemptPhase,
    AttemptReacquireRequested,
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    ReacquisitionReady,
    ReleaseDependencyBlocked,
    ReleaseDependencyObserved,
    RestoreRevision,
    RetainRevision,
    RetentionRequired,
    RetireRequested,
    ScopeAdmissionReopened,
    ScopeReopenAdmitted,
    ScopeReopenRequested,
    SnapshotAndRetain,
)
from .types.common import (
    AttemptRef,
    CompletionStatus,
    ContractValidationError,
    DecisionId,
    ExecuteRegisteredOperation,
    InvocationRef,
    LifecycleClass,
    ObservationStatus,
    OperationId,
    ReleaseDependency,
    RequestId,
    ResourceId,
    RunId,
    Scope,
    SessionId,
)
from .types.evaluation import (
    ContinuationPhase,
    ContinuationRetireRequested,
    ContinuationScopeReopened,
    JobsDrainRequested,
    RegisteredOwnedJob,
)
from .types.intents import InspectRequest
from .types.kernel import AreaChange, DecisionCompleted
from .types.scheduling import (
    AttemptReopenRequest,
    AttemptReopenRequested,
    QueueEntryRetired,
    SlotChargeEnded,
    SlotReleased,
)
from .types.scope_reopen import ScopedAdmissionReopenOutcome
from .types.sessions import (
    CloseSession,
    DispatchTurn,
    EnsureSession,
    ResumeSessionTurn,
    SessionDrainRequested,
)
from .types.settlement import OwnershipSettled
from .types.strategy import Accepted, Operation, StartAttempt, Withdraw

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent, AttemptsState, AttemptView
    from .types.common import Observation
    from .types.evaluation import OwnedJob
    from .types.intents import ChildLease, Intent, Request
    from .types.kernel import AttemptsContext, Signal
    from .types.sessions import Invocation, SessionSpec
    from .types.settlement import Settlement


def _ref(owner: AttemptView) -> AttemptRef:
    return AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)


def _scope(owner: AttemptView) -> Scope:
    return Scope(owner=owner.attempt_id, generation=owner.generation)


def _identity(owner: AttemptView, suffix: str) -> RequestId:
    if owner.closure is None:
        raise ContractValidationError("closure", "retirement authority is required")
    return RequestId(root=f"{owner.closure.authority.root}:{suffix}")


def _intent(context: AttemptsContext, identity: RequestId) -> Intent | None:
    return next((row for row in context.intents.intents if row.request_id == identity), None)


def _pending(owner: AttemptView, context: AttemptsContext) -> Settlement | None:
    return next((row for row in context.settlement.pending if row.attempt == _ref(owner)), None)


def _released(observation: Observation | None) -> bool:
    return bool(
        observation is not None
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        and observation.terminal
        and observation.released
        and observation.children_complete
    )


def _child_released(child: ChildLease) -> bool:
    observation = child.observation
    return bool(
        _released(observation)
        and observation is not None
        and observation.scope == child.scope
        and observation.resource_id == child.resource_id
        and observation.request_id in child.source_requests
    )


def _nonownership(observation: Observation) -> bool:
    return (
        not observation.accepted
        and observation.resource_id is None
        and observation.status
        in (ObservationStatus.REJECTED, ObservationStatus.FAILED, ObservationStatus.CANCELLED)
        and _released(observation)
    )


def _typed_job_released(job: OwnedJob | RegisteredOwnedJob) -> bool:
    observation = job.observation
    source = job.request_id if isinstance(job, RegisteredOwnedJob) else job.submission_id
    if job.resource_id is None and (observation is None or not _nonownership(observation)):
        return False
    return bool(
        _released(observation)
        and observation is not None
        and observation.scope == job.scope
        and observation.resource_id == job.resource_id
        and observation.request_id == source
    )


def _resource_released(context: AttemptsContext, scope: Scope, resource: ResourceId) -> bool:
    return any(
        child.resource_id == resource and child.scope == scope and _child_released(child)
        for child in context.intents.children
    ) or any(
        job.resource_id == resource and job.scope == scope and _typed_job_released(job)
        for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
    )


def _session_released(owner: AttemptView, context: AttemptsContext, identity: SessionId) -> bool:
    if owner.closure is None:
        return False
    session = next(
        (row for row in context.sessions.sessions if row.spec.session_id == identity), None
    )
    if session is None:
        return False
    return any(
        isinstance(row.request, CloseSession)
        and row.request.session_id == identity
        and row.request.scope == _scope(owner)
        and row.request.admission_id == owner.closure.admission_id
        and row.request.request_id == row.request_id
        and row.observation is not None
        and row.observation.request_id == row.request_id
        and row.observation.scope == row.request.scope
        and row.observation.admission_id == row.request.admission_id
        and (
            (session.resource_id is not None and row.observation.resource_id == session.resource_id)
            or (session.resource_id is None and _nonownership(row.observation))
        )
        and _released(row.observation)
        for row in context.intents.intents
    )


def _session_dependencies(
    owner: AttemptView, context: AttemptsContext
) -> tuple[ReleaseDependency, ...]:
    edges = list(owner.release_dependencies)
    scope = _scope(owner)
    for session in context.sessions.sessions:
        edge = ReleaseDependency(kind="session", identity=session.spec.session_id)
        if (
            session.scope == scope
            and not _session_released(owner, context, session.spec.session_id)
            and edge not in edges
        ):
            edges.append(edge)
    return tuple(edges)


def _discover(owner: AttemptView, context: AttemptsContext) -> AttemptView:
    """Install all known child ownership before considering graph completion."""
    edges = list(_session_dependencies(owner, context))
    scope = _scope(owner)
    for child in context.intents.children:
        edge = ReleaseDependency(kind="job", identity=child.resource_id)
        if child.scope == scope and not _child_released(child) and edge not in edges:
            edges.append(edge)
    for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs):
        if job.scope != scope or _typed_job_released(job):
            continue
        edge = (
            ReleaseDependency(kind="job", identity=job.resource_id)
            if job.resource_id is not None
            else ReleaseDependency(kind="request", identity=job.request_id)
        )
        if edge not in edges:
            edges.append(edge)
    for intent in context.intents.intents:
        observation = intent.observation
        if intent.request.scope != scope or observation is None:
            continue
        for resource in observation.children:
            edge = ReleaseDependency(kind="job", identity=resource)
            if not _resource_released(context, scope, resource) and edge not in edges:
                edges.append(edge)
    return owner.model_copy(update={"release_dependencies": tuple(edges)})


def _invocation_drained(
    owner: AttemptView, context: AttemptsContext, invocation: Invocation
) -> bool:
    observation = invocation.observation
    if (
        owner.closure is None
        or observation is None
        or not _released(observation)
        or (observation.resource_id is None and not _nonownership(observation))
        or observation.scope != invocation.scope
        or observation.admission_id is None
        or invocation.invocation.session_id != invocation.turn.session.session_id
        or invocation.invocation.invocation_id != invocation.turn.invocation_id
    ):
        return False
    session = next(
        (
            row
            for row in context.sessions.sessions
            if row.spec.session_id == invocation.invocation.session_id
        ),
        None,
    )
    if (
        session is None
        or session.spec != invocation.turn.session
        or session.generation != invocation.invocation.generation
        or not _session_scope_owned(owner, context, session.scope, session.spec)
    ):
        return False
    intent = _intent(context, observation.request_id)
    if (
        intent is None
        or intent.observation != observation
        or intent.request.request_id != intent.request_id
        or intent.request.scope != invocation.scope
        or intent.request.admission_id != observation.admission_id
    ):
        return False
    request = intent.request
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return request.turn == invocation.turn and invocation.registered_operation is None
    if isinstance(request, ExecuteRegisteredOperation):
        return _registered_invocation_proved(context, invocation, request)
    return False


def _registered_invocation_proved(
    context: AttemptsContext, invocation: Invocation, request: ExecuteRegisteredOperation
) -> bool:
    if invocation.registered_operation != request.operation_id:
        return False
    return any(
        isinstance(row.feedback, Accepted)
        and row.feedback.decision_id == row.decision_id
        and isinstance(row.decision, Operation)
        and row.decision.decision_id == row.decision_id
        and row.decision_id == request.decision_id
        and row.decision.scope == request.scope
        and row.decision.normalized_turn == invocation.turn
        and row.decision.registered_wire == request.operation
        and request.operation_id == OperationId(root=f"operation:{row.decision_id.root}")
        for row in context.run.receipts
    )


def _writers_drained(owner: AttemptView, context: AttemptsContext) -> bool:
    if owner.closure is None:
        return False
    fence = _intent(context, owner.closure.authority)
    if (
        fence is None
        or not isinstance(fence.request, CloseAttemptScope)
        or fence.request.request_id != owner.closure.authority
        or fence.request.attempt != _ref(owner)
        or fence.request.scope != _scope(owner)
        or fence.request.admission_id != owner.closure.admission_id
        or fence.observation is None
        or fence.observation.request_id != owner.closure.authority
        or fence.observation.scope != _scope(owner)
        or fence.observation.admission_id != owner.closure.admission_id
        or fence.observation.status != ObservationStatus.SUCCEEDED
        or not fence.observation.terminal
        or not fence.observation.children_complete
    ):
        return False
    if any(
        edge.kind in ("session", "request", "job", "operation")
        for edge in owner.release_dependencies
    ):
        return False
    return all(
        _invocation_drained(owner, context, row)
        for row in context.sessions.invocations
        if row.scope == _scope(owner)
    )


def _retention_request(
    owner: AttemptView, context: AttemptsContext, event: RetentionRequired | None
) -> Request | None:
    if owner.closure is None:
        raise ContractValidationError("closure", "retirement authority is required")
    pending = _pending(owner, context)
    if event is not None and pending is None and owner.closure.disposition == "settle":
        raise ContractValidationError(
            "retention", "retention requires the pending accepted settlement"
        )
    if owner.closure.disposition == "cancel":
        retention, revision = "discard", None
    elif owner.closure.disposition == "park":
        retention, revision = "wip", None
    elif pending is not None:
        retention, revision = pending.retention, pending.candidate
    else:
        return None
    if event is not None and (event.retention != retention or event.revision != revision):
        return None
    if retention == "discard":
        return DiscardWorkspace(
            request_id=_identity(owner, "discard"),
            depends_on=(owner.closure.authority,),
            scope=_scope(owner),
            attempt=_ref(owner),
            admission_id=owner.closure.admission_id,
            deadline_at=context.run.deadline_at,
        )
    if revision is not None:
        return RetainRevision(
            request_id=_identity(owner, "retention"),
            depends_on=(owner.closure.authority,),
            scope=_scope(owner),
            attempt=_ref(owner),
            admission_id=owner.closure.admission_id,
            deadline_at=context.run.deadline_at,
            retention=retention,
            revision=revision,
        )
    return SnapshotAndRetain(
        request_id=_identity(owner, "retention"),
        depends_on=(owner.closure.authority,),
        scope=_scope(owner),
        attempt=_ref(owner),
        admission_id=owner.closure.admission_id,
        deadline_at=context.run.deadline_at,
        retention=retention,
    )


def _request_payload_matches(expected: Request, recorded: Request) -> bool:
    return (
        expected.model_copy(
            update={
                "decision_id": recorded.decision_id,
                "decision_dependencies": recorded.decision_dependencies,
            }
        )
        == recorded
    )


def _retention_done(owner: AttemptView, context: AttemptsContext) -> bool:
    if owner.closure is None:
        return False
    expected = _retention_request(owner, context, None)
    if expected is None or expected.request_id is None:
        return False
    intent = _intent(context, expected.request_id)
    if intent is None or intent.observation is None:
        return False
    if not _request_payload_matches(expected, intent.request):
        return False
    return _retention_observed(owner, intent)


def _retention_observed(owner: AttemptView, intent: Intent) -> bool:
    if owner.closure is None or intent.observation is None:
        return False
    observation = intent.observation
    if (
        observation.request_id != intent.request_id
        or intent.request.scope != _scope(owner)
        or intent.request.admission_id != owner.closure.admission_id
        or observation.scope != _scope(owner)
        or observation.admission_id != owner.closure.admission_id
    ):
        return False
    if isinstance(intent.request, DiscardWorkspace):
        return observation.status == ObservationStatus.SUCCEEDED and _released(observation)
    if observation.status != ObservationStatus.SUCCEEDED or not observation.terminal:
        return False
    if not isinstance(intent.request, RetainRevision | SnapshotAndRetain):
        return False
    return any(
        row.request_id == intent.request_id
        and row.retention == intent.request.retention
        and (
            not isinstance(intent.request, RetainRevision)
            or row.revision == intent.request.revision
        )
        for row in owner.checkpoints
    )


def _withdrawal_completed(owner: AttemptView, context: AttemptsContext) -> tuple[Signal, ...]:
    closure = owner.closure
    if closure is None or closure.disposition == "settle":
        return ()
    return tuple(
        DecisionCompleted(decision_id=row.decision_id, status=CompletionStatus.SUCCEEDED)
        for row in context.run.receipts
        if isinstance(row.feedback, Accepted)
        and row.feedback.decision_id == row.decision_id
        and isinstance(row.decision, Withdraw)
        and row.decision.decision_id == row.decision_id
        and row.decision.target == _ref(owner)
        and row.decision.disposition.kind == closure.disposition
        and closure.authority == RequestId(root=f"withdraw:{row.decision_id.root}")
        and row.completion is None
    )


def _progress(
    owner: AttemptView, context: AttemptsContext, event: RetentionRequired | None = None
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    owner = _discover(owner, context)
    requests: tuple[Request, ...] = ()
    if owner.closure is None:
        raise ContractValidationError("closure", "retirement authority is required")
    if _writers_drained(owner, context):
        request = _retention_request(owner, context, event)
        if request is not None and request.request_id is not None:
            edge = ReleaseDependency(kind="workspace", identity=request.request_id)
            existing = _intent(context, request.request_id)
            if existing is not None and not _request_payload_matches(request, existing.request):
                raise ContractValidationError(
                    "request_id", "retention identity conflicts with recorded disposition"
                )
            if existing is None and edge not in owner.release_dependencies:
                owner = owner.model_copy(
                    update={"release_dependencies": (*owner.release_dependencies, edge)}
                )
                requests = (request,)
    if (
        owner.release_dependencies
        or not _retention_done(owner, context)
        or not _writers_drained(owner, context)
    ):
        return owner, (), requests
    closure = owner.closure
    if closure is None:
        return owner, (), requests
    phase = AttemptPhase.PARKED if closure.disposition == "park" else AttemptPhase.TERMINAL
    owner = owner.model_copy(update={"phase": phase})
    return (
        owner,
        (
            OwnershipSettled(attempt=_ref(owner), released=True),
            SlotReleased(attempt=_ref(owner), admission_id=closure.admission_id),
            *_withdrawal_completed(owner, context),
        ),
        requests,
    )


def _queued_admission(owner: AttemptView, context: AttemptsContext) -> DecisionId | None:
    return next(
        (
            row.decision_id
            for row in context.run.receipts
            if isinstance(row.feedback, Accepted)
            and row.feedback.decision_id == row.decision_id
            and isinstance(row.decision, StartAttempt)
            and row.decision.decision_id == row.decision_id
            and row.decision.attempt_id == owner.attempt_id
            and row.decision.scope.generation == owner.generation
        ),
        None,
    )


def _episode_authorized(owner: AttemptView, context: AttemptsContext, identity: DecisionId) -> bool:
    for receipt in context.run.receipts:
        if (
            receipt.decision_id != identity
            or not isinstance(receipt.feedback, Accepted)
            or receipt.feedback.decision_id != identity
            or receipt.decision is None
            or receipt.decision.decision_id != identity
        ):
            continue
        decision = receipt.decision
        if isinstance(decision, StartAttempt):
            return (
                decision.attempt_id == owner.attempt_id
                and decision.scope.owner == context.run.run_id
                and decision.scope.generation == owner.generation
            )
        if isinstance(decision, Operation) and decision.normalized_scope_reopen is not None:
            return decision.normalized_scope_reopen.attempt == _ref(owner)
    return False


def _start_closure(
    owner: AttemptView, context: AttemptsContext, event: RetireRequested
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    if (
        owner.closure is not None and owner.closure.admission_id == event.admission_id
    ) or owner.phase in (AttemptPhase.TERMINAL, AttemptPhase.PARKED):
        return owner, (), ()
    admission = owner.admission_id
    if admission is None and owner.phase == AttemptPhase.QUEUED:
        admission = _queued_admission(owner, context)
    if (
        admission != event.admission_id
        or not _episode_authorized(owner, context, event.admission_id)
        or any(row.attempt == event.attempt for row in context.settlement.settlements)
    ):
        return owner, (), ()
    if event.disposition != "settle" and _pending(owner, context) is not None:
        return owner, (), ()
    scope = _scope(owner)
    edges = list(owner.release_dependencies)
    edges.extend(ReleaseDependency(kind="session", identity=value) for value in owner.sessions)
    edges.extend(
        ReleaseDependency(kind="job", identity=row.resource_id)
        for row in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
        if row.scope == scope and row.resource_id is not None and not _typed_job_released(row)
    )
    edges.extend(
        ReleaseDependency(kind="request", identity=row.request_id)
        for row in context.evaluation.registered_jobs
        if row.scope == scope and row.resource_id is None and not row.released
    )
    edges.extend(
        ReleaseDependency(kind="request", identity=value) for value in owner.pending_intents
    )
    edges.append(ReleaseDependency(kind="request", identity=event.authority))
    closure = AttemptClosure(
        disposition=event.disposition,
        requested_at=event.requested_at,
        authority=event.authority,
        admission_id=event.admission_id,
    )
    queued = owner.phase == AttemptPhase.QUEUED
    owner = owner.model_copy(
        update={
            "closure": closure,
            "phase": AttemptPhase.CLOSING,
            "release_dependencies": tuple(dict.fromkeys(edges)),
        }
    )
    signals: list[Signal] = [
        SessionDrainRequested(
            attempt=event.attempt, authority=event.authority, disposition=event.disposition
        ),
        JobsDrainRequested(scope=scope, authority=event.authority, disposition=event.disposition),
    ]
    if queued:
        signals.append(QueueEntryRetired(attempt=event.attempt))
    else:
        signals.append(
            SlotChargeEnded(
                attempt=event.attempt, admission_id=event.admission_id, ended_at=event.requested_at
            )
        )
    for continuation in context.evaluation.continuations:
        invocation = next(
            (
                row
                for row in context.sessions.invocations
                if row.invocation == continuation.invocation
            ),
            None,
        )
        if invocation is not None and invocation.scope == scope:
            signals.append(
                ContinuationRetireRequested(
                    continuation_id=continuation.continuation_id,
                    disposition="park" if event.disposition == "park" else "cancel",
                    park_authority=event.authority if event.disposition == "park" else None,
                )
            )
    request = CloseAttemptScope(
        request_id=event.authority,
        scope=scope,
        attempt=event.attempt,
        admission_id=event.admission_id,
        deadline_at=context.run.deadline_at,
    )
    owner, more, requests = _progress(owner, context)
    return owner, (*signals, *more), (request, *requests)


def _job_proof(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> bool:
    observation = event.observation
    if event.dependency.identity != observation.resource_id or not _released(observation):
        return False
    for child in context.intents.children:
        if child.resource_id == observation.resource_id:
            return (
                child.scope == _scope(owner)
                and child.observation == observation
                and observation.request_id in child.source_requests
            )
    for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs):
        if job.resource_id == observation.resource_id:
            source = job.request_id if hasattr(job, "request_id") else job.submission_id
            return (
                job.scope == _scope(owner)
                and job.observation == observation
                and source == observation.request_id
            )
    return False


def _proof_matches(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> bool:
    observation = event.observation
    if (
        observation.scope != _scope(owner)
        or owner.closure is None
        or observation.admission_id != owner.closure.admission_id
    ):
        return False
    edge = event.dependency
    if edge.kind == "job":
        return _job_proof(owner, context, event)
    intent = _intent(context, observation.request_id)
    if intent is None or intent.request.admission_id != owner.closure.admission_id:
        return False
    return _intent_proof(owner, context, event, intent)


def _intent_proof(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved, intent: Intent
) -> bool:
    edge, observation = event.dependency, event.observation
    if (
        intent.request.scope != observation.scope
        or intent.observation != observation
        or (
            intent.lifecycle == LifecycleClass.OWNED_JOB
            and observation.resource_id is None
            and not _nonownership(observation)
        )
    ):
        return False
    if edge.kind in ("request", "workspace") and edge.identity != observation.request_id:
        return False
    if edge.kind == "session":
        return getattr(intent.request, "session_id", None) == edge.identity and _released(
            observation
        )
    if isinstance(intent.request, CloseAttemptScope):
        return (
            observation.status == ObservationStatus.SUCCEEDED
            and observation.terminal
            and observation.children_complete
        )
    if isinstance(intent.request, RetainRevision | SnapshotAndRetain):
        return (
            observation.status == ObservationStatus.SUCCEEDED
            and observation.terminal
            and _retention_done(owner, context)
        )
    return _released(observation) and (
        edge.kind != "operation" or getattr(intent.request, "operation_id", None) == edge.identity
    )


def _continuation_owned(
    owner: AttemptView, context: AttemptsContext, invocation: InvocationRef
) -> bool:
    return any(
        row.invocation == invocation and row.scope == _scope(owner)
        for row in context.sessions.invocations
    )


def _reopen(
    owner: AttemptView, context: AttemptsContext, event: ScopeReopenRequested
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    norm = event.normalization
    closure = owner.closure
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.continuation_id == norm.continuation_id
        ),
        None,
    )
    receipt = next(
        (
            row
            for row in context.run.receipts
            if event.request.request_id == RequestId(root=f"operation:{row.decision_id.root}")
        ),
        None,
    )
    if (
        owner.phase != AttemptPhase.PARKED
        or closure is None
        or closure.disposition != "park"
        or closure.authority != norm.park_authority
        or owner.release_dependencies
        or owner.checkpoint is None
        or continuation is None
        or not _continuation_owned(owner, context, continuation.invocation)
        or continuation.phase != ContinuationPhase.REOPENING
        or continuation.park_authority != norm.park_authority
        or set(continuation.cancelled_resolutions) != set(norm.resolved_cancelled_jobs)
        or event.request.request_id is None
        or receipt is None
        or not isinstance(receipt.feedback, Accepted)
        or receipt.feedback.decision_id != receipt.decision_id
        or not isinstance(receipt.decision, Operation)
        or receipt.decision.normalized_scope_reopen != norm
    ):
        return owner, (), ()
    canonical = _reopen_request(owner, context, event.request.request_id)
    if canonical is None or not _original_request_matches(canonical, event.request):
        return owner, (), ()
    return (
        owner,
        (
            AttemptReopenRequested(
                request=AttemptReopenRequest(
                    decision_id=receipt.decision_id,
                    request_id=event.request.request_id,
                    attempt=_ref(owner),
                )
            ),
        ),
        (),
    )


def _original_request_matches(
    canonical: ExecuteRegisteredOperation, request: ExecuteRegisteredOperation
) -> bool:
    if request.decision_id not in (None, canonical.decision_id):
        return False
    if request.decision_dependencies not in ((), canonical.decision_dependencies):
        return False
    return (
        canonical.model_copy(
            update={
                "decision_id": request.decision_id,
                "decision_dependencies": request.decision_dependencies,
            }
        )
        == request
    )


def _reopen_request(
    owner: AttemptView, context: AttemptsContext, identity: RequestId
) -> ExecuteRegisteredOperation | None:
    for receipt in context.run.receipts:
        decision = receipt.decision
        if (
            not isinstance(receipt.feedback, Accepted)
            or receipt.feedback.decision_id != receipt.decision_id
            or not isinstance(decision, Operation)
            or decision.decision_id != receipt.decision_id
            or decision.registered_wire is None
            or decision.normalized_scope_reopen is None
            or decision.normalized_scope_reopen.attempt != _ref(owner)
            or owner.closure is None
            or decision.normalized_scope_reopen.park_authority != owner.closure.authority
            or identity != RequestId(root=f"operation:{receipt.decision_id.root}")
        ):
            continue
        return ExecuteRegisteredOperation(
            request_id=identity,
            operation_id=OperationId(root=identity.root),
            scope=decision.scope,
            deadline_at=decision.deadline_at,
            decision_id=receipt.decision_id,
            decision_dependencies=decision.depends_on,
            operation=decision.registered_wire,
            retry_limit=context.run.limits.max_retries,
        )
    return None


def _admitted(
    owner: AttemptView, context: AttemptsContext, event: ScopeReopenAdmitted
) -> tuple[AttemptView, tuple[Signal, ...]]:
    request = _reopen_request(owner, context, event.request_id)
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.reopen_authority == event.request_id
        ),
        None,
    )
    if (
        owner.phase != AttemptPhase.PARKED
        or owner.closure is None
        or owner.checkpoint is None
        or request is None
        or request.decision_id != event.admission_id
        or continuation is None
        or continuation.park_authority != owner.closure.authority
        or continuation.phase != ContinuationPhase.REOPENING
        or not _continuation_owned(owner, context, continuation.invocation)
    ):
        return owner, ()
    checkpoint = owner.checkpoint
    if checkpoint is None:
        return owner, ()
    owner = owner.model_copy(
        update={"phase": AttemptPhase.ACQUIRING, "admission_id": event.admission_id}
    )
    return owner, (
        AttemptReacquireRequested(
            attempt=_ref(owner),
            continuation_id=continuation.continuation_id,
            request_id=event.request_id,
            admission_id=event.admission_id,
            base=checkpoint,
        ),
    )


def _session_scope_owned(
    owner: AttemptView, context: AttemptsContext, scope: Scope, spec: SessionSpec
) -> bool:
    return scope == _scope(owner) or (
        isinstance(scope.owner, RunId)
        and scope.owner == context.run.run_id
        and scope.generation == context.run.generation
        and spec.policy == "reuse"
        and spec.lifetime == "owner"
    )


def _session_reattached(owner: AttemptView, context: AttemptsContext, identity: SessionId) -> bool:
    if owner.closure is None:
        return False
    session = next(
        (row for row in context.sessions.sessions if row.spec.session_id == identity), None
    )
    if (
        session is None
        or not _session_scope_owned(owner, context, session.scope, session.spec)
        or session.resource_id is None
        or not session.accepted
    ):
        return False
    for intent in context.intents.intents:
        request, observation = intent.request, intent.observation
        if (
            not isinstance(request, EnsureSession)
            or request.spec != session.spec
            or request.scope != session.scope
            or request.admission_id != owner.admission_id
            or request.required_resource != session.resource_id
            or observation is None
            or observation.request_id != intent.request_id
            or observation.scope != session.scope
            or observation.admission_id != owner.admission_id
            or observation.resource_id != request.required_resource
            or observation.status != ObservationStatus.SUCCEEDED
            or not observation.accepted
            or not observation.terminal
        ):
            continue
        return any(
            isinstance(old.request, EnsureSession)
            and old.request.spec.session_id == identity
            and old.request.scope == session.scope
            and (
                old.request.admission_id == owner.closure.admission_id
                or (isinstance(session.scope.owner, RunId) and old.request.admission_id is None)
            )
            and old.observation is not None
            and old.observation.request_id == old.request_id
            and old.observation.scope == session.scope
            and old.observation.admission_id == old.request.admission_id
            and old.observation.resource_id == request.required_resource
            and old.observation.accepted
            and old.observation.status == ObservationStatus.SUCCEEDED
            for old in context.intents.intents
        )
    return False


def _reacquired(owner: AttemptView, context: AttemptsContext) -> bool:
    if any(
        row.request_id in owner.pending_intents
        and isinstance(row.request, EnsureWorkspace | RestoreRevision)
        and row.request.admission_id == owner.admission_id
        for row in context.intents.intents
    ):
        return False
    workspace = any(
        isinstance(row.request, RestoreRevision)
        and row.request.revision == owner.checkpoint
        and row.request.attempt == _ref(owner)
        and row.request.scope == _scope(owner)
        and row.request.admission_id == owner.admission_id
        and row.request.decision_id == owner.admission_id
        and row.observation is not None
        and row.observation.request_id == row.request_id
        and row.observation.scope == _scope(owner)
        and row.observation.admission_id == owner.admission_id
        and row.observation.status == ObservationStatus.SUCCEEDED
        and row.observation.accepted
        and row.observation.terminal
        for row in context.intents.intents
    )
    sessions = any(
        row.attempt == _ref(owner)
        and row.admission_id == owner.admission_id
        and row.scope == _scope(owner)
        and row.phase == "ready"
        and row.session_ids == owner.sessions
        for row in context.sessions.acquisition_groups
    )
    continuations = tuple(
        row
        for row in context.evaluation.continuations
        if owner.closure is not None
        and row.park_authority == owner.closure.authority
        and row.phase == ContinuationPhase.REOPENING
        and _continuation_owned(owner, context, row.invocation)
    )
    return (
        workspace
        and sessions
        and bool(continuations)
        and all(_session_reattached(owner, context, identity) for identity in owner.sessions)
        and all(
            row.next_invocation.session_id == row.invocation.session_id
            and row.next_invocation.generation == row.invocation.generation
            and row.next_invocation.invocation_id != row.invocation.invocation_id
            and _session_reattached(owner, context, row.invocation.session_id)
            for row in continuations
        )
    )


def _ready(
    owner: AttemptView, context: AttemptsContext, event: ReacquisitionReady
) -> tuple[AttemptView, tuple[Request, ...]]:
    request = _reopen_request(owner, context, event.request_id)
    if (
        owner.phase != AttemptPhase.ACQUIRING
        or owner.admission_id != event.admission_id
        or owner.closure is None
        or request is None
        or request.decision_id != event.admission_id
        or not _reacquired(owner, context)
        or not any(
            row.phase == ContinuationPhase.REOPENING
            and row.reopen_authority == event.request_id
            and row.park_authority == owner.closure.authority
            and _continuation_owned(owner, context, row.invocation)
            for row in context.evaluation.continuations
        )
    ):
        return owner, ()
    return owner, (request.model_copy(update={"admission_id": event.admission_id}),)


def _reopen_observation_proved(
    owner: AttemptView, context: AttemptsContext, event: ScopeAdmissionReopened, intent: Intent
) -> bool:
    canonical = _reopen_request(owner, context, event.observation.request_id)
    if canonical is None or intent.request != canonical.model_copy(
        update={"admission_id": owner.admission_id}
    ):
        return False
    if intent.observation != event.observation:
        return False
    if event.admission == "unknown" or event.observation.status == ObservationStatus.UNKNOWN:
        return True
    outcome = intent.outcome
    return (
        intent.observation == event.observation
        and isinstance(outcome, ScopedAdmissionReopenOutcome)
        and outcome.scope == _scope(owner)
        and outcome.admission == event.admission
        and intent.outcome_is_registered
        and _reacquired(owner, context)
    )


def _reopened(
    owner: AttemptView, context: AttemptsContext, event: ScopeAdmissionReopened
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.continuation_id == event.continuation_id
        ),
        None,
    )
    intent = next(
        (
            row
            for row in context.intents.intents
            if isinstance(row.request, ExecuteRegisteredOperation)
            and row.request.operation_id == event.operation_id
        ),
        None,
    )
    if (
        owner.phase != AttemptPhase.ACQUIRING
        or owner.closure is None
        or owner.closure.authority != event.park_authority
        or continuation is None
        or continuation.park_authority != event.park_authority
        or continuation.reopen_authority != event.observation.request_id
        or intent is None
        or intent.request_id != event.observation.request_id
        or intent.request.decision_id != owner.admission_id
        or event.observation.scope != intent.request.scope
        or event.observation.admission_id != owner.admission_id
        or continuation.phase != ContinuationPhase.REOPENING
        or not _continuation_owned(owner, context, continuation.invocation)
        or not _reopen_observation_proved(owner, context, event, intent)
    ):
        return owner, (), ()
    if event.admission == "unknown" or event.observation.status == ObservationStatus.UNKNOWN:
        request = InspectRequest(
            request_id=_inspection_identity(intent.request_id, None, event.observation.sequence),
            scope=intent.request.scope,
            target=intent.request_id,
            admission_id=owner.admission_id,
            deadline_at=context.run.deadline_at,
        )
        existing = _intent(context, request.request_id) if request.request_id is not None else None
        return owner, (), (request,) if existing is None else ()
    if (
        event.admission != "reopened"
        or event.observation.status != ObservationStatus.SUCCEEDED
        or not event.observation.accepted
        or not event.observation.terminal
    ):
        return owner, (), ()
    owner = owner.model_copy(update={"phase": AttemptPhase.ACTIVE, "closure": None})
    return (
        owner,
        (
            ContinuationScopeReopened(
                continuation_id=event.continuation_id,
                park_authority=event.park_authority,
                observation=event.observation,
            ),
        ),
        (),
    )


def _retention_event(
    owner: AttemptView, context: AttemptsContext, event: RetentionRequired
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    pending = _pending(owner, context)
    if (
        pending is None
        or pending.retention != event.retention
        or pending.candidate != event.revision
    ):
        return owner, (), ()
    if owner.closure is None:
        admission = owner.admission_id
        if admission is None and owner.phase == AttemptPhase.QUEUED:
            admission = _queued_admission(owner, context)
        if admission is None:
            return owner, (), ()
        return _start_closure(
            owner,
            context,
            RetireRequested(
                attempt=_ref(owner),
                disposition="settle",
                authority=RequestId(root=f"{pending.settlement_id.root}:close"),
                admission_id=admission,
                requested_at=context.run.now_at,
            ),
        )
    if owner.phase in (AttemptPhase.CLOSING, AttemptPhase.BLOCKED):
        return _progress(owner, context, event)
    return owner, (), ()


def _inspection_identity(
    target: RequestId, resource: ResourceId | None, sequence: int
) -> RequestId:
    domain = "root" if resource is None else "child"
    value = "" if resource is None else resource.root
    return RequestId(
        root=f"inspect:{len(target.root)}:{target.root}:{domain}:{len(value)}:{value}:{sequence}"
    )


def _inspect_unknown(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> tuple[Request, ...]:
    observation = event.observation
    intent = _intent(context, observation.request_id)
    if (
        owner.closure is None
        or event.dependency not in owner.release_dependencies
        or observation.scope != _scope(owner)
        or observation.admission_id != owner.closure.admission_id
        or intent is None
        or intent.request.scope != observation.scope
        or intent.request.admission_id != observation.admission_id
    ):
        return ()
    child = next(
        (row for row in context.intents.children if row.resource_id == observation.resource_id),
        None,
    )
    if intent.observation != observation and not (
        child is not None
        and child.scope == observation.scope
        and child.observation == observation
        and observation.request_id in child.source_requests
    ):
        return ()
    request = InspectRequest(
        request_id=_inspection_identity(
            observation.request_id, observation.resource_id, observation.sequence
        ),
        target=observation.request_id,
        resource_id=observation.resource_id,
        scope=observation.scope,
        admission_id=observation.admission_id,
        deadline_at=context.run.deadline_at,
    )
    identity = request.request_id
    if identity is None:
        return ()
    existing = _intent(context, identity)
    if existing is not None:
        if existing.request != request.model_copy(
            update={
                "decision_id": existing.request.decision_id,
                "decision_dependencies": existing.request.decision_dependencies,
                "depends_on": existing.request.depends_on,
            }
        ):
            raise ContractValidationError(
                "request_id", "inspection identity conflicts with recorded request"
            )
        return ()
    return (request,)


def _closure_event(
    owner: AttemptView,
    context: AttemptsContext,
    event: RetentionRequired | ReleaseDependencyObserved | ReleaseDependencyBlocked,
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    signals: tuple[Signal, ...] = ()
    requests: tuple[Request, ...] = ()
    if isinstance(event, RetentionRequired):
        owner, signals, requests = _retention_event(owner, context, event)
    elif isinstance(event, ReleaseDependencyObserved) and owner.phase in (
        AttemptPhase.CLOSING,
        AttemptPhase.BLOCKED,
    ):
        owner = _discover(owner, context)
        if event.observation.status == ObservationStatus.UNKNOWN:
            return owner, (), _inspect_unknown(owner, context, event)
        if event.dependency in owner.release_dependencies and _proof_matches(owner, context, event):
            owner = owner.model_copy(
                update={
                    "release_dependencies": tuple(
                        edge for edge in owner.release_dependencies if edge != event.dependency
                    )
                }
            )
            owner, signals, requests = _progress(owner, context)
    elif (
        isinstance(event, ReleaseDependencyBlocked)
        and owner.closure is not None
        and event.authority == owner.closure.authority
        and event.dependency in owner.release_dependencies
    ):
        owner = owner.model_copy(update={"phase": AttemptPhase.BLOCKED})
        signals = (OwnershipSettled(attempt=_ref(owner), released=False, blocked=True),)
    return owner, signals, requests


def advance(
    state: AttemptsState, context: AttemptsContext, event: AttemptsEvent
) -> AreaChange[AttemptsState]:
    """Reduce only retirement-owned fields; all cross-area work uses typed signals."""
    target = (
        event.normalization.attempt
        if isinstance(event, ScopeReopenRequested)
        else getattr(event, "attempt", None)
    )
    owner = next((row for row in state.attempts if _ref(row) == target), None)
    if owner is None:
        return AreaChange(state=state)
    signals: tuple[Signal, ...] = ()
    requests: tuple[Request, ...] = ()
    if isinstance(event, RetireRequested):
        owner, signals, requests = _start_closure(owner, context, event)
    elif isinstance(event, ScopeReopenRequested):
        owner, signals, requests = _reopen(owner, context, event)
    elif isinstance(
        event, RetentionRequired | ReleaseDependencyObserved | ReleaseDependencyBlocked
    ):
        owner, signals, requests = _closure_event(owner, context, event)
    elif isinstance(event, ScopeReopenAdmitted):
        owner, signals = _admitted(owner, context, event)
    elif isinstance(event, ReacquisitionReady):
        owner, requests = _ready(owner, context, event)
    elif isinstance(event, ScopeAdmissionReopened):
        owner, signals, requests = _reopened(owner, context, event)
    return AreaChange(
        state=state.model_copy(
            update={
                "attempts": tuple(owner if _ref(row) == target else row for row in state.attempts)
            }
        ),
        signals=signals,
        requests=requests,
    )
