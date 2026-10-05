"""Durable attempt closure, release dependencies and guarded scope reopening."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import (
    AttemptCheckpoint,
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
    WorkspaceObserved,
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
    SessionPhase,
)
from .types.settlement import OwnershipSettled
from .types.strategy import Accepted, Operation, StartAttempt, Withdraw

if TYPE_CHECKING:
    from .types.attempts import AttemptsEvent, AttemptsState, AttemptView
    from .types.common import ContinuationId, Observation, RevisionRef
    from .types.evaluation import OwnedJob
    from .types.intents import ChildLease, Intent, Request
    from .types.kernel import AttemptsContext, Signal
    from .types.sessions import Invocation, SessionSpec, SessionView
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
    rows = tuple(row for row in context.intents.intents if row.request_id == identity)
    return rows[0] if len(rows) == 1 and rows[0].request.request_id == identity else None


def _pending(owner: AttemptView, context: AttemptsContext) -> Settlement | None:
    rows = tuple(row for row in context.settlement.pending if row.attempt == _ref(owner))
    return rows[0] if len(rows) == 1 else None


def _released(observation: Observation | None) -> bool:
    return bool(
        observation is not None
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        and observation.terminal
        and observation.released
        and observation.children_complete
    )


def _child_released(context: AttemptsContext, child: ChildLease) -> bool:
    marks = child.observation_watermarks
    return bool(
        child.watermark_history_complete
        and marks
        and child.observation is not None
        and {mark.source_request for mark in marks} == set(child.source_requests)
        and any(mark.observation == child.observation for mark in marks)
        and all(
            mark.observation.request_id == mark.source_request
            and mark.observation.scope == child.scope
            and mark.observation.resource_id == child.resource_id
            and _child_source_proved(context, mark.source_request, mark.observation)
            and _released(mark.observation)
            for mark in marks
        )
    )


def _child_source_proved(
    context: AttemptsContext, source: RequestId, observation: Observation
) -> bool:
    intent = _intent(context, source)
    return bool(
        intent is not None
        and intent.request.scope == observation.scope
        and intent.request.admission_id is not None
        and intent.request.admission_id == observation.admission_id
    )


def _nonownership(observation: Observation) -> bool:
    return (
        not observation.accepted
        and observation.resource_id is None
        and observation.status
        in (ObservationStatus.REJECTED, ObservationStatus.FAILED, ObservationStatus.CANCELLED)
        and _released(observation)
    )


def _typed_job_released(context: AttemptsContext, job: OwnedJob | RegisteredOwnedJob) -> bool:
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
        and _child_source_proved(context, source, observation)
    )


def _resource_released(context: AttemptsContext, scope: Scope, resource: ResourceId) -> bool:
    children = tuple(
        child
        for child in context.intents.children
        if child.resource_id == resource and child.scope == scope
    )
    if children:
        return all(_child_released(context, child) for child in children)
    jobs = tuple(
        job
        for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
        if job.resource_id == resource and job.scope == scope
    )
    return bool(jobs) and all(_typed_job_released(context, job) for job in jobs)


def _session_released(owner: AttemptView, context: AttemptsContext, identity: SessionId) -> bool:
    if owner.closure is None:
        return False
    session = next(
        (row for row in context.sessions.sessions if row.spec.session_id == identity), None
    )
    if session is None:
        return False
    if _session_nonowned(owner, context, session):
        return True
    return any(
        _intent(context, row.request_id) == row
        and isinstance(row.request, CloseSession)
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


def _session_nonowned(owner: AttemptView, context: AttemptsContext, session: SessionView) -> bool:
    if (
        owner.closure is None
        or session.phase != SessionPhase.TERMINAL
        or session.accepted
        or session.resource_id is not None
        or session.pending_intents
        or session.scope != _scope(owner)
    ):
        return False
    acquisitions = tuple(
        row
        for row in context.intents.intents
        if isinstance(row.request, EnsureSession)
        and row.request.spec.session_id == session.spec.session_id
    )
    return bool(acquisitions) and all(
        _intent(context, row.request_id) == row
        and isinstance(row.request, EnsureSession)
        and row.request.scope == session.scope
        and row.request.spec == session.spec
        and row.request.admission_id == owner.closure.admission_id
        and row.request.request_id == row.request_id
        and row.observation is not None
        and row.observation.request_id == row.request_id
        and row.observation.scope == row.request.scope
        and row.observation.admission_id == row.request.admission_id
        and _nonownership(row.observation)
        for row in acquisitions
    )


def _reusable_session(context: AttemptsContext, session: SessionView) -> bool:
    return (
        isinstance(session.scope.owner, RunId)
        and session.scope.owner == context.run.run_id
        and session.scope.generation == context.run.generation
        and session.spec.policy == "reuse"
        and session.spec.lifetime == "owner"
    )


def _invocation_source(context: AttemptsContext, invocation: Invocation) -> RequestId | None:
    for intent in context.intents.intents:
        request = intent.request
        if request.scope != invocation.scope:
            continue
        if (
            isinstance(request, DispatchTurn | ResumeSessionTurn)
            and request.turn == invocation.turn
        ):
            return intent.request_id
        if isinstance(request, ExecuteRegisteredOperation) and _registered_invocation_proved(
            context, invocation, request
        ):
            return intent.request_id
    return None


def _session_dependencies(
    owner: AttemptView, context: AttemptsContext
) -> tuple[ReleaseDependency, ...]:
    edges = [
        edge
        for edge in owner.release_dependencies
        if edge.kind != "session"
        or not isinstance(edge.identity, SessionId)
        or not _session_released(owner, context, edge.identity)
    ]
    scope = _scope(owner)
    for session in context.sessions.sessions:
        if _reusable_session(context, session):
            edges.extend(_reusable_dependencies(owner, context, session.spec.session_id))
        elif (
            session.scope == scope or session.spec.session_id in owner.sessions
        ) and not _session_released(owner, context, session.spec.session_id):
            edges.append(ReleaseDependency(kind="session", identity=session.spec.session_id))
    known = {session.spec.session_id for session in context.sessions.sessions}
    edges.extend(
        ReleaseDependency(kind="session", identity=identity)
        for identity in owner.sessions
        if identity not in known
    )
    return tuple(dict.fromkeys(edges))


def _reusable_dependencies(
    owner: AttemptView, context: AttemptsContext, identity: SessionId
) -> tuple[ReleaseDependency, ...]:
    edges = []
    for invocation in context.sessions.invocations:
        if invocation.scope != _scope(owner) or invocation.invocation.session_id != identity:
            continue
        if _invocation_drained(owner, context, invocation):
            continue
        source = _invocation_source(context, invocation)
        if source is None:
            # A missing canonical turn source cannot prove this writer drained.
            edges.append(ReleaseDependency(kind="session", identity=identity))
        else:
            edges.append(ReleaseDependency(kind="request", identity=source))
    return tuple(edges)


def _manifest_dependencies(
    context: AttemptsContext,
    scope: Scope,
    edges: tuple[ReleaseDependency, ...],
    resources: tuple[ResourceId, ...],
) -> tuple[ReleaseDependency, ...]:
    collected = list(edges)
    for resource in resources:
        edge = ReleaseDependency(kind="job", identity=resource)
        if not _resource_released(context, scope, resource) and edge not in collected:
            collected.append(edge)
    return tuple(collected)


def _child_dependencies(
    owner: AttemptView, context: AttemptsContext, previous: tuple[ReleaseDependency, ...]
) -> tuple[ReleaseDependency, ...]:
    scope = _scope(owner)
    edges = list(previous)
    for child in context.intents.children:
        edge = ReleaseDependency(kind="job", identity=child.resource_id)
        if child.scope == scope:
            if not _child_released(context, child) and edge not in edges:
                edges.append(edge)
            edges = list(
                _manifest_dependencies(
                    context,
                    scope,
                    tuple(edges),
                    tuple(
                        resource
                        for observation in (
                            *(mark.observation for mark in child.observation_watermarks),
                            *((child.observation,) if child.observation is not None else ()),
                        )
                        for resource in observation.children
                    ),
                )
            )
    return tuple(edges)


def _discover(owner: AttemptView, context: AttemptsContext) -> AttemptView:
    """Install all known child ownership before considering graph completion."""
    edges = list(_child_dependencies(owner, context, _session_dependencies(owner, context)))
    scope = _scope(owner)
    for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs):
        if job.scope != scope:
            continue
        resources = (*job.children, *(job.observation.children if job.observation else ()))
        edges = list(_manifest_dependencies(context, scope, tuple(edges), resources))
        if _typed_job_released(context, job):
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
        and row.decision.registered_turn == row.decision.normalized_turn
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


def _discard_request(owner: AttemptView, context: AttemptsContext) -> DiscardWorkspace | None:
    retention = _retention_request(owner, context, None)
    if retention is None or owner.closure is None:
        return None
    if isinstance(retention, DiscardWorkspace):
        return retention
    return DiscardWorkspace(
        request_id=_identity(owner, "discard"),
        attempt=_ref(owner),
        scope=_scope(owner),
        admission_id=owner.closure.admission_id,
        deadline_at=context.run.deadline_at,
        depends_on=(owner.closure.authority, _identity(owner, "retention")),
    )


def _disposal_done(owner: AttemptView, context: AttemptsContext) -> bool:
    expected = _discard_request(owner, context)
    if expected is None or expected.request_id is None:
        return False
    intent = _intent(context, expected.request_id)
    if (
        intent is None
        or intent.observation is None
        or not _request_payload_matches(expected, intent.request)
    ):
        return False
    observation = intent.observation
    return (
        observation.request_id == intent.request_id
        and observation.scope == expected.scope
        and observation.admission_id == expected.admission_id
        and observation.status == ObservationStatus.SUCCEEDED
        and _released(observation)
    )


def _prepare_workspace_request(
    owner: AttemptView, context: AttemptsContext, request: Request | None
) -> tuple[AttemptView, tuple[Request, ...]]:
    if request is None or request.request_id is None:
        return owner, ()
    edge = ReleaseDependency(kind="workspace", identity=request.request_id)
    existing = _intent(context, request.request_id)
    if existing is not None and not _request_payload_matches(request, existing.request):
        raise ContractValidationError(
            "request_id", "retention identity conflicts with recorded disposition"
        )
    if existing is not None or edge in owner.release_dependencies:
        return owner, ()
    return owner.model_copy(update={"release_dependencies": (*owner.release_dependencies, edge)}), (
        request,
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
    if (
        observation.status != ObservationStatus.SUCCEEDED
        or not observation.terminal
        or not observation.children_complete
        or (observation.resource_id is not None and not observation.released)
    ):
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


def _reopen_failed_completed(owner: AttemptView, context: AttemptsContext) -> tuple[Signal, ...]:
    closure = owner.closure
    if closure is None or closure.disposition != "cancel":
        return ()
    return tuple(
        DecisionCompleted(decision_id=row.decision_id, status=CompletionStatus.FAILED)
        for row in context.run.receipts
        if isinstance(row.feedback, Accepted)
        and row.feedback.decision_id == row.decision_id
        and isinstance(row.decision, Operation)
        and row.decision.decision_id == row.decision_id
        and row.decision.normalized_scope_reopen is not None
        and row.decision.registered_scope_reopen == row.decision.normalized_scope_reopen
        and row.decision.normalized_scope_reopen.attempt == _ref(owner)
        and closure.admission_id == row.decision_id
        and closure.authority == RequestId(root=f"reopen-failed:operation:{row.decision_id.root}")
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
        request = (
            _discard_request(owner, context)
            if _retention_done(owner, context)
            else _retention_request(owner, context, event)
        )
        owner, requests = _prepare_workspace_request(owner, context, request)
    if (
        owner.release_dependencies
        or not _retention_done(owner, context)
        or not _disposal_done(owner, context)
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
            *_reopen_failed_completed(owner, context),
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
            return (
                decision.scope.owner == context.run.run_id
                and decision.scope.generation == context.run.generation
                and decision.registered_scope_reopen == decision.normalized_scope_reopen
                and decision.normalized_scope_reopen.attempt == _ref(owner)
            )
    return False


def _retire_queued_reopen(
    owner: AttemptView, context: AttemptsContext, event: RetireRequested
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    identity = RequestId(root=f"operation:{event.admission_id.root}")
    request = _reopen_request(owner, context, identity)
    continuation = next(
        (
            row
            for row in context.evaluation.continuations
            if row.reopen_authority == identity
            and row.phase == ContinuationPhase.REOPENING
            and _continuation_owned(owner, context, row.invocation)
        ),
        None,
    )
    if (
        request is None
        or continuation is None
        or owner.closure is None
        or event.admission_id == owner.admission_id
        or event.disposition == "settle"
    ):
        return owner, (), ()
    signals: tuple[Signal, ...] = (
        QueueEntryRetired(attempt=_ref(owner), admission_id=event.admission_id),
        DecisionCompleted(decision_id=event.admission_id, status=CompletionStatus.CANCELLED),
        ContinuationRetireRequested(
            continuation_id=continuation.continuation_id,
            disposition=event.disposition,
            park_authority=owner.closure.authority if event.disposition == "park" else None,
        ),
    )
    discovered = _discover(owner, context)
    if discovered.release_dependencies:
        # Keep the historical fence processable while retiring the new queue entry.
        # A second closure cannot be persisted in the frozen owner contract.
        # Fail the exact withdrawal rather than lose it behind the historical fence.
        signals += _queued_withdrawal_failed(owner, context, event)
        return discovered.model_copy(update={"phase": AttemptPhase.CLOSING}), signals, ()
    closure = AttemptClosure(
        disposition=event.disposition,
        requested_at=event.requested_at,
        authority=event.authority,
        admission_id=event.admission_id,
    )
    completed = owner.model_copy(update={"closure": closure})
    signals += _withdrawal_completed(completed, context)
    if event.disposition == "cancel":
        owner = completed.model_copy(update={"phase": AttemptPhase.TERMINAL})
    return owner, signals, ()


def _queued_withdrawal_failed(
    owner: AttemptView, context: AttemptsContext, event: RetireRequested
) -> tuple[Signal, ...]:
    return tuple(
        DecisionCompleted(decision_id=row.decision_id, status=CompletionStatus.FAILED)
        for row in context.run.receipts
        if isinstance(row.feedback, Accepted)
        and row.feedback.decision_id == row.decision_id
        and isinstance(row.decision, Withdraw)
        and row.decision.decision_id == row.decision_id
        and row.decision.target == _ref(owner)
        and row.decision.disposition.kind == event.disposition
        and event.authority == RequestId(root=f"withdraw:{row.decision_id.root}")
        and row.completion is None
    )


def _start_closure(
    owner: AttemptView, context: AttemptsContext, event: RetireRequested
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    if owner.phase == AttemptPhase.PARKED:
        return _retire_queued_reopen(owner, context, event)
    if (
        owner.closure is not None and owner.closure.admission_id == event.admission_id
    ) or owner.phase == AttemptPhase.TERMINAL:
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
    pending = _pending(owner, context)
    if event.disposition == "settle" and (
        pending is None or event.authority != RequestId(root=f"{pending.settlement_id.root}:close")
    ):
        return owner, (), ()
    if event.disposition != "settle" and any(
        row.attempt == _ref(owner) for row in context.settlement.pending
    ):
        return owner, (), ()
    scope = _scope(owner)
    edges = list(owner.release_dependencies)
    edges.extend(
        ReleaseDependency(kind="job", identity=row.resource_id)
        for row in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
        if row.scope == scope
        and row.resource_id is not None
        and not _typed_job_released(context, row)
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
        signals.append(QueueEntryRetired(attempt=event.attempt, admission_id=event.admission_id))
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
                and _known_child_observation(owner, context, observation)
                and observation.request_id in child.source_requests
                and _child_released(context, child)
            )
    for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs):
        if job.resource_id == observation.resource_id:
            source = job.request_id if hasattr(job, "request_id") else job.submission_id
            return (
                job.scope == _scope(owner)
                and job.observation == observation
                and source == observation.request_id
                and _typed_job_released(context, job)
            )
    return False


def _proof_matches(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> bool:
    observation = event.observation
    if observation.scope != _scope(owner) or owner.closure is None:
        return False
    edge = event.dependency
    if edge.kind == "job":
        return _job_proof(owner, context, event)
    if observation.admission_id != owner.closure.admission_id:
        return False
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
    return _request_release_proof(owner, context, event, intent)


def _request_release_proof(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved, intent: Intent
) -> bool:
    edge, observation = event.dependency, event.observation
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
    if isinstance(intent.request, DiscardWorkspace):
        return _disposal_done(owner, context)
    if isinstance(intent.request, SnapshotAndRetain) and intent.request.invocation is not None:
        # An invocation's own checkpoint, not the closure's retention: any conclusive
        # answer ends the wait. A failed snapshot retains nothing, and closure still
        # retains the workspace itself, so the attempt must not wait on it forever.
        return observation.terminal and observation.status not in (
            ObservationStatus.UNKNOWN,
            ObservationStatus.PENDING,
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


def _park_revision(owner: AttemptView) -> RevisionRef | None:
    if owner.closure is None or owner.closure.disposition != "park":
        return None
    checkpoints = tuple(
        row for row in owner.checkpoints if row.request_id == _identity(owner, "retention")
    )
    if len(checkpoints) != 1 or checkpoints[0].retention != "wip":
        return None
    return checkpoints[0].revision


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
        or _discover(owner, context).release_dependencies
        or _park_revision(owner) is None
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
    owner: AttemptView, context: AttemptsContext, identity: RequestId, *, cleanup: bool = False
) -> ExecuteRegisteredOperation | None:
    for receipt in context.run.receipts:
        decision = receipt.decision
        if (
            not isinstance(receipt.feedback, Accepted)
            or receipt.feedback.decision_id != receipt.decision_id
            or (
                not cleanup
                and receipt.completion in (CompletionStatus.FAILED, CompletionStatus.CANCELLED)
            )
            or not isinstance(decision, Operation)
            or decision.decision_id != receipt.decision_id
            or decision.registered_wire is None
            or decision.scope.owner != context.run.run_id
            or decision.scope.generation != context.run.generation
            or decision.normalized_scope_reopen is None
            or decision.registered_scope_reopen != decision.normalized_scope_reopen
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
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    request = _reopen_request(owner, context, event.request_id)
    checkpoint = _park_revision(owner)
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
        or checkpoint is None
        or request is None
        or request.decision_id != event.admission_id
        or continuation is None
        or continuation.continuation_id != _request_continuation(context, event.request_id)
        or continuation.park_authority != owner.closure.authority
        or continuation.phase != ContinuationPhase.REOPENING
        or not _continuation_owned(owner, context, continuation.invocation)
    ):
        return owner, (), ()
    if checkpoint is None:
        return owner, (), ()
    owner = owner.model_copy(
        update={"phase": AttemptPhase.ACQUIRING, "admission_id": event.admission_id}
    )
    if _discover(owner, context).release_dependencies:
        return _start_closure(
            owner,
            context,
            RetireRequested(
                attempt=_ref(owner),
                disposition="cancel",
                authority=RequestId(root=f"reopen-failed:{event.request_id.root}"),
                admission_id=event.admission_id,
                requested_at=context.run.now_at,
            ),
        )
    return (
        owner,
        (
            AttemptReacquireRequested(
                attempt=_ref(owner),
                continuation_id=continuation.continuation_id,
                request_id=event.request_id,
                admission_id=event.admission_id,
                base=checkpoint,
            ),
        ),
        (),
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
        or session.phase
        not in (SessionPhase.IDLE, SessionPhase.CHECKPOINTED, SessionPhase.SUSPENDED)
        or session.pending_intents
        or session.resource_id is None
        or not session.accepted
    ):
        return False
    for intent in context.intents.intents:
        request, observation = intent.request, intent.observation
        if (
            not isinstance(request, EnsureSession)
            or _intent(context, intent.request_id) != intent
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
            _intent(context, old.request_id) == old
            and isinstance(old.request, EnsureSession)
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


def _request_continuation(context: AttemptsContext, identity: RequestId) -> ContinuationId | None:
    return next(
        (
            row.decision.normalized_scope_reopen.continuation_id
            for row in context.run.receipts
            if isinstance(row.decision, Operation)
            and row.decision.normalized_scope_reopen is not None
            and identity == RequestId(root=f"operation:{row.decision_id.root}")
        ),
        None,
    )


def _reacquired(owner: AttemptView, context: AttemptsContext, identity: RequestId) -> bool:
    if any(
        row.request_id in owner.pending_intents
        and isinstance(row.request, EnsureWorkspace | RestoreRevision)
        and row.request.admission_id == owner.admission_id
        for row in context.intents.intents
    ):
        return False
    workspace = any(
        _intent(context, row.request_id) == row
        and isinstance(row.request, RestoreRevision)
        and row.request.revision == _park_revision(owner)
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
        and row.reopen_authority == identity
        and row.continuation_id == _request_continuation(context, identity)
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
        or not _reacquired(owner, context, event.request_id)
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
    canonical = _reopen_request(owner, context, event.observation.request_id, cleanup=True)
    if canonical is None or intent.request != canonical.model_copy(
        update={"admission_id": owner.admission_id}
    ):
        return False
    if intent.observation != event.observation:
        return False
    if event.admission == "unknown" or event.observation.status == ObservationStatus.UNKNOWN:
        return True
    if event.observation.status == ObservationStatus.PENDING:
        return False
    if event.observation.terminal and event.observation.status in (
        ObservationStatus.FAILED,
        ObservationStatus.REJECTED,
        ObservationStatus.CANCELLED,
    ):
        return True
    outcome = intent.outcome
    return (
        intent.observation == event.observation
        and isinstance(outcome, ScopedAdmissionReopenOutcome)
        and outcome.scope == _scope(owner)
        and outcome.admission == event.admission
        and intent.outcome_is_registered
        and (
            _reopen_receipt_failed(context, owner.admission_id)
            or _reacquired(owner, context, event.observation.request_id)
        )
    )


def _reopen_receipt_failed(context: AttemptsContext, admission: DecisionId | None) -> bool:
    return any(
        row.decision_id == admission
        and row.completion in (CompletionStatus.FAILED, CompletionStatus.CANCELLED)
        for row in context.run.receipts
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
        or continuation.continuation_id
        != _request_continuation(context, event.observation.request_id)
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
    admission = owner.admission_id
    if admission is None:
        return owner, (), ()
    if event.observation.terminal and (
        event.admission == "closed"
        or _reopen_receipt_failed(context, admission)
        or event.observation.status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        return _start_closure(
            owner,
            context,
            RetireRequested(
                attempt=_ref(owner),
                disposition="cancel",
                authority=RequestId(root=f"reopen-failed:{intent.request_id.root}"),
                admission_id=admission,
                requested_at=context.run.now_at,
            ),
        )
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
            DecisionCompleted(decision_id=admission, status=CompletionStatus.SUCCEEDED),
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


def _known_child_observation(
    owner: AttemptView, context: AttemptsContext, observation: Observation
) -> bool:
    return any(
        child.scope == _scope(owner)
        and child.resource_id == observation.resource_id
        and observation.request_id in child.source_requests
        and (
            child.observation == observation
            or any(
                mark.source_request == observation.request_id and mark.observation == observation
                for mark in child.observation_watermarks
            )
        )
        for child in context.intents.children
    )


def _known_job_observation(
    owner: AttemptView, context: AttemptsContext, observation: Observation
) -> bool:
    return any(
        job.scope == _scope(owner)
        and job.resource_id == observation.resource_id
        and job.observation == observation
        and (job.request_id if isinstance(job, RegisteredOwnedJob) else job.submission_id)
        == observation.request_id
        for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
    )


def _historical_child_owned(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> bool:
    observation = event.observation
    return (
        event.dependency.kind == "job"
        and event.dependency.identity == observation.resource_id
        and _child_source_proved(context, observation.request_id, observation)
        and (
            _known_child_observation(owner, context, observation)
            or _known_job_observation(owner, context, observation)
        )
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
        or (
            observation.admission_id != owner.closure.admission_id
            and not _historical_child_owned(owner, context, event)
        )
        or intent is None
        or intent.request.scope != observation.scope
        or intent.request.admission_id != observation.admission_id
    ):
        return ()
    if (
        intent.observation != observation
        and not _known_child_observation(owner, context, observation)
        and not _known_job_observation(owner, context, observation)
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


def is_retention_acknowledgement(context: AttemptsContext, event: WorkspaceObserved) -> bool:
    """Whether a workspace observation answers a closure's retention request."""
    intent = _intent(context, event.observation.request_id)
    return (
        intent is not None
        and isinstance(intent.request, RetainRevision | SnapshotAndRetain)
        and getattr(intent.request, "invocation", None) is None
    )


def retention_acknowledged(
    owner: AttemptView, context: AttemptsContext, event: WorkspaceObserved
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
    """Record the closure's retention checkpoint from the workspace's typed acknowledgement.

    The workspace acknowledgement names the retained revision. `_retention_done`
    reads `owner.checkpoints`, and invocation checkpoints are the only other
    writer, so without this the settle-close retention never completes. Once the
    checkpoint exists, the release edge of the retention request is re-evaluated
    with the ledger's recorded observation, exactly as if it had arrived after.
    """
    observation = event.observation
    if owner.phase not in (AttemptPhase.CLOSING, AttemptPhase.BLOCKED):
        return owner, (), ()
    recorded = _record_retention(owner, context, observation, event.revision)
    if recorded == owner:
        return owner, (), ()
    edge = ReleaseDependency(kind="workspace", identity=observation.request_id)
    return _closure_event(
        recorded,
        context,
        ReleaseDependencyObserved(attempt=_ref(owner), dependency=edge, observation=observation),
    )


def _record_retention(
    owner: AttemptView,
    context: AttemptsContext,
    observation: Observation,
    reported: RevisionRef | None,
) -> AttemptView:
    """Append the checkpoint for the retention this closure expects, when exactly proven.

    The acknowledgement must answer exactly the retention request the closure
    expects, in the closure's episode, report a terminal success with its children
    complete, and name the retained revision.
    """
    expected = _retention_request(owner, context, None)
    intent = _intent(context, observation.request_id)
    if (
        owner.closure is None
        or expected is None
        or expected.request_id != observation.request_id
        or not isinstance(expected, RetainRevision | SnapshotAndRetain)
        or intent is None
        or not _request_payload_matches(expected, intent.request)
        or intent.observation != observation
        or observation.scope != _scope(owner)
        or observation.admission_id != owner.closure.admission_id
        or observation.status != ObservationStatus.SUCCEEDED
        or not observation.terminal
        or not observation.children_complete
        or any(row.request_id == expected.request_id for row in owner.checkpoints)
    ):
        return owner
    revision = expected.revision if isinstance(expected, RetainRevision) else reported
    if revision is None or (isinstance(expected, RetainRevision) and reported != revision):
        return owner
    checkpoint = AttemptCheckpoint(
        invocation=None,
        request_id=expected.request_id,
        revision=revision,
        retention=expected.retention,
    )
    return owner.model_copy(update={"checkpoints": (*owner.checkpoints, checkpoint)})


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
        # Discovery lists only unreleased edges, so a release that Sessions or Evaluation
        # already recorded is absent here; the proof, not the list, authorizes progress.
        if _proof_matches(owner, context, event):
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
    elif isinstance(event, WorkspaceObserved):
        owner, signals, requests = retention_acknowledged(owner, context, event)
    elif isinstance(event, ScopeReopenAdmitted):
        owner, signals, requests = _admitted(owner, context, event)
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
