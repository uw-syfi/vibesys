"""Durable attempt closure and release dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._acquisition_stall import is_stalled_acquisition
from ._attempt_terminal import vacant_workspace_request
from ._deadlines import cleanup
from ._submission_receipts import same_conclusion
from .types.attempts import (
    AttemptCheckpoint,
    AttemptClosure,
    AttemptPhase,
    CloseAttemptScope,
    DiscardWorkspace,
    ReleaseDependencyBlocked,
    ReleaseDependencyObserved,
    RetainRevision,
    RetentionRequired,
    RetireRequested,
    SnapshotAndRetain,
    WorkspaceObserved,
)
from .types.common import (
    AttemptRef,
    CompletionStatus,
    ContractValidationError,
    DecisionId,
    ExecuteRegisteredOperation,
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
    ContinuationRetireRequested,
    JobsDrainRequested,
    RegisteredOwnedJob,
)
from .types.kernel import AreaChange, DecisionCompleted
from .types.records import InspectRequest
from .types.scheduling import (
    QueueEntryRetired,
    SlotChargeEnded,
    SlotReleased,
)
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
    from .types.common import Observation, RevisionRef
    from .types.evaluation import OwnedJob
    from .types.kernel import AttemptsContext, Signal
    from .types.records import Request, RequestRecord
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


def _record(context: AttemptsContext, identity: RequestId) -> RequestRecord | None:
    # The ledger holds every record of the run, and closing an attempt looks up one identity per
    # record, so a scan here made the step cost the square of the ledger; `find` reads the index.
    rows = context.records.find(identity)
    return rows[0] if len(rows) == 1 and rows[0].request.request_id == identity else None


def _pending(owner: AttemptView, context: AttemptsContext) -> Settlement | None:
    attempt = _ref(owner)
    rows = tuple(row for row in context.settlement.pending if row.attempt == attempt)
    return rows[0] if len(rows) == 1 else None


def _released(observation: Observation | None) -> bool:
    return bool(
        observation is not None
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        and observation.terminal
        and observation.released
        and observation.children_complete
    )


def _child_source_proved(
    context: AttemptsContext, source: RequestId, observation: Observation
) -> bool:
    record = _record(context, source)
    return bool(
        record is not None
        and record.request.scope == observation.scope
        and record.request.admission_id is not None
        and record.request.admission_id == observation.admission_id
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
    jobs = tuple(
        job
        for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
        if job.resource_id == resource and job.scope == scope
    )
    # The job's own ledger proves the resource released.
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
        _record(context, row.request_id) == row
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
        for row in context.records.records
    )


def _session_nonowned(owner: AttemptView, context: AttemptsContext, session: SessionView) -> bool:
    if (
        owner.closure is None
        or session.phase != SessionPhase.TERMINAL
        or session.accepted
        or session.resource_id is not None
        or session.pending_records
        or session.scope != _scope(owner)
    ):
        return False
    acquisitions = tuple(
        row
        for row in context.records.records
        if isinstance(row.request, EnsureSession)
        and row.request.spec.session_id == session.spec.session_id
    )
    return bool(acquisitions) and all(
        _record(context, row.request_id) == row
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
    for record in context.records.records:
        request = record.request
        if request.scope != invocation.scope:
            continue
        if (
            isinstance(request, DispatchTurn | ResumeSessionTurn)
            and request.turn == invocation.turn
        ):
            return record.request_id
        if isinstance(request, ExecuteRegisteredOperation) and _registered_invocation_proved(
            context, invocation, request
        ):
            return record.request_id
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


def _discover(owner: AttemptView, context: AttemptsContext) -> AttemptView:
    """Install all known child ownership before considering graph completion."""
    edges = list(_session_dependencies(owner, context))
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
    for record in context.records.records:
        observation = record.observation
        if record.request.scope != scope or observation is None:
            continue
        for resource in observation.children:
            edge = ReleaseDependency(kind="job", identity=resource)
            if not _resource_released(context, scope, resource) and edge not in edges:
                edges.append(edge)
    return owner.model_copy(update={"release_dependencies": tuple(edges)})


def _invocation_drained(
    owner: AttemptView, context: AttemptsContext, invocation: Invocation
) -> bool:
    # A turn core ended because nothing could carry its acquisition never reached an executor,
    # so there is no dispatch to prove it drained with.
    if owner.closure is not None and is_stalled_acquisition(invocation):
        return True
    return _dispatched_invocation_drained(owner, context, invocation)


def _dispatched_invocation_drained(
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
    record = _record(context, observation.request_id)
    if (
        record is None
        or record.observation != observation
        or record.request.request_id != record.request_id
        or record.request.scope != invocation.scope
        or record.request.admission_id != observation.admission_id
    ):
        return False
    request = record.request
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
    fence = _record(context, owner.closure.authority)
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
    if (
        event is not None
        and pending is None
        and owner.closure.disposition == "settle"
        and not owner.closure.retention_refused
    ):
        raise ContractValidationError(
            "retention", "retention requires the pending accepted settlement"
        )
    if owner.closure.disposition == "cancel" or owner.closure.retention_refused:
        retention, revision = "discard", None
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
        )
    if revision is not None:
        return RetainRevision(
            request_id=_identity(owner, "retention"),
            depends_on=(owner.closure.authority,),
            scope=_scope(owner),
            attempt=_ref(owner),
            admission_id=owner.closure.admission_id,
            retention=retention,
            revision=revision,
        )
    return SnapshotAndRetain(
        request_id=_identity(owner, "retention"),
        depends_on=(owner.closure.authority,),
        scope=_scope(owner),
        attempt=_ref(owner),
        admission_id=owner.closure.admission_id,
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
        depends_on=(owner.closure.authority, _identity(owner, "retention")),
    )


def _disposal_done(owner: AttemptView, context: AttemptsContext) -> bool:
    expected = _discard_request(owner, context)
    if expected is None or expected.request_id is None:
        return False
    record = _record(context, expected.request_id)
    if (
        record is None
        or record.observation is None
        or not _request_payload_matches(expected, record.request)
    ):
        return False
    observation = record.observation
    if owner.closure is not None and owner.closure.discard_refused:
        return _refused_by_owner(owner, record)
    return (
        observation.request_id == record.request_id
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
    existing = _record(context, request.request_id)
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
    return expected.model_copy(update={"decision_id": recorded.decision_id}) == recorded


_REFUSALS = (ObservationStatus.REJECTED, ObservationStatus.FAILED)


def _refused_by_owner(owner: AttemptView, record: RequestRecord) -> bool:
    """Whether the workspace owner conclusively answered this closure request "no"."""
    observation = record.observation
    return (
        owner.closure is not None
        and observation is not None
        and observation.terminal
        and observation.status in _REFUSALS
        and observation.scope == _scope(owner)
        and observation.admission_id == owner.closure.admission_id
    )


def _refusal(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> AttemptClosure | None:
    """The closure after the owner refused the retention or discard it asked for, if it did.

    A request answered "no" holds nothing, so it cannot be waited on for ever. A refused
    retention means the candidate cannot be kept: the attempt settles failed and the workspace
    is discarded. A refused discard leaves nothing more to ask, so the attempt ends with it.
    """
    closure = owner.closure
    record = _record(context, event.observation.request_id)
    if (
        closure is None
        or record is None
        or event.dependency.identity != record.request_id
        or record.observation != event.observation
        or not _refused_by_owner(owner, record)
    ):
        return None
    request = record.request
    if event.dependency.kind != "workspace":
        return None
    if (
        isinstance(request, RetainRevision | SnapshotAndRetain)
        and getattr(request, "invocation", None) is None
        and request.request_id == _identity(owner, "retention")
        and closure.disposition == "settle"
        and not closure.retention_refused
    ):
        return closure.model_copy(update={"retention_refused": True})
    if (
        isinstance(request, DiscardWorkspace)
        and request.request_id == _identity(owner, "discard")
        and not closure.discard_refused
    ):
        return closure.model_copy(update={"discard_refused": True})
    return None


def _retention_done(owner: AttemptView, context: AttemptsContext) -> bool:
    if owner.closure is None:
        return False
    expected = _retention_request(owner, context, None)
    if expected is None or expected.request_id is None:
        return False
    record = _record(context, expected.request_id)
    if record is None or record.observation is None:
        return False
    if not _request_payload_matches(expected, record.request):
        return False
    return _retention_observed(owner, record)


def _retention_observed(owner: AttemptView, record: RequestRecord) -> bool:
    if owner.closure is None or record.observation is None:
        return False
    observation = record.observation
    if (
        observation.request_id != record.request_id
        or record.request.scope != _scope(owner)
        or record.request.admission_id != owner.closure.admission_id
        or observation.scope != _scope(owner)
        or observation.admission_id != owner.closure.admission_id
    ):
        return False
    if isinstance(record.request, DiscardWorkspace):
        return (
            _refused_by_owner(owner, record)
            if owner.closure.discard_refused
            else observation.status == ObservationStatus.SUCCEEDED and _released(observation)
        )
    if (
        observation.status != ObservationStatus.SUCCEEDED
        or not observation.terminal
        or not observation.children_complete
        or (observation.resource_id is not None and not observation.released)
    ):
        return False
    if not isinstance(record.request, RetainRevision | SnapshotAndRetain):
        return False
    return any(
        row.request_id == record.request_id
        and row.retention == record.request.retention
        and (
            not isinstance(record.request, RetainRevision)
            or row.revision == record.request.revision
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
    owner = owner.model_copy(update={"phase": AttemptPhase.TERMINAL})
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
    return False


def _start_closure(
    owner: AttemptView, context: AttemptsContext, event: RetireRequested
) -> tuple[AttemptView, tuple[Signal, ...], tuple[Request, ...]]:
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
        ReleaseDependency(kind="request", identity=value)
        for value in owner.pending_records
        if not vacant_workspace_request(_record(context, value))
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
                ContinuationRetireRequested(continuation_id=continuation.continuation_id)
            )
    request = CloseAttemptScope(
        request_id=event.authority,
        scope=scope,
        attempt=event.attempt,
        admission_id=event.admission_id,
    )
    owner, more, requests = _progress(owner, context)
    return owner, (*signals, *more), (request, *requests)


def _job_proof(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> bool:
    observation = event.observation
    if event.dependency.identity != observation.resource_id or not _released(observation):
        return False
    for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs):
        if job.resource_id == observation.resource_id:
            source = job.request_id if hasattr(job, "request_id") else job.submission_id
            return (
                job.scope == _scope(owner)
                and same_conclusion(job.observation, observation)
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
    record = _record(context, observation.request_id)
    if record is None or record.request.admission_id != owner.closure.admission_id:
        return False
    return _record_proof(owner, context, event, record)


def _record_proof(
    owner: AttemptView,
    context: AttemptsContext,
    event: ReleaseDependencyObserved,
    record: RequestRecord,
) -> bool:
    edge, observation = event.dependency, event.observation
    if (
        record.request.scope != observation.scope
        or record.observation != observation
        or (
            record.lifecycle == LifecycleClass.OWNED_JOB
            and observation.resource_id is None
            and not _nonownership(observation)
        )
    ):
        return False
    if edge.kind in ("request", "workspace") and edge.identity != observation.request_id:
        return False
    return _request_release_proof(owner, context, event, record)


def _request_release_proof(
    owner: AttemptView,
    context: AttemptsContext,
    event: ReleaseDependencyObserved,
    record: RequestRecord,
) -> bool:
    edge, observation = event.dependency, event.observation
    if edge.kind == "session":
        return getattr(record.request, "session_id", None) == edge.identity and _released(
            observation
        )
    if isinstance(record.request, CloseAttemptScope):
        return (
            observation.status == ObservationStatus.SUCCEEDED
            and observation.terminal
            and observation.children_complete
        )
    if isinstance(record.request, DiscardWorkspace):
        return _disposal_done(owner, context)
    if isinstance(record.request, SnapshotAndRetain) and record.request.invocation is not None:
        # An invocation's own checkpoint, not the closure's retention: any conclusive
        # answer ends the wait. A failed snapshot retains nothing, and closure still
        # retains the workspace itself, so the attempt must not wait on it forever.
        return observation.terminal and observation.status not in (
            ObservationStatus.UNKNOWN,
            ObservationStatus.PENDING,
        )
    if isinstance(record.request, RetainRevision | SnapshotAndRetain):
        return (
            observation.status == ObservationStatus.SUCCEEDED
            and observation.terminal
            and _retention_done(owner, context)
        )
    return vacant_workspace_request(record) or (
        _released(observation)
        and (
            edge.kind != "operation"
            or getattr(record.request, "operation_id", None) == edge.identity
        )
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


def _known_job_observation(
    owner: AttemptView, context: AttemptsContext, observation: Observation
) -> bool:
    return any(
        job.scope == _scope(owner)
        and job.resource_id == observation.resource_id
        and same_conclusion(job.observation, observation)
        and (job.request_id if isinstance(job, RegisteredOwnedJob) else job.submission_id)
        == observation.request_id
        for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
    )


def _historical_job_owned(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> bool:
    observation = event.observation
    return (
        event.dependency.kind == "job"
        and event.dependency.identity == observation.resource_id
        and _child_source_proved(context, observation.request_id, observation)
        and _known_job_observation(owner, context, observation)
    )


def _inspect_unknown(
    owner: AttemptView, context: AttemptsContext, event: ReleaseDependencyObserved
) -> tuple[Request, ...]:
    observation = event.observation
    record = _record(context, observation.request_id)
    if (
        owner.closure is None
        or event.dependency not in owner.release_dependencies
        or observation.scope != _scope(owner)
        or (
            observation.admission_id != owner.closure.admission_id
            and not _historical_job_owned(owner, context, event)
        )
        or record is None
        or record.request.scope != observation.scope
        or record.request.admission_id != observation.admission_id
    ):
        return ()
    if record.observation != observation and not _known_job_observation(
        owner, context, observation
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
    )
    identity = request.request_id
    if identity is None:
        return ()
    existing = _record(context, identity)
    if existing is not None:
        if existing.request != request.model_copy(
            update={
                "decision_id": existing.request.decision_id,
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
    record = _record(context, event.observation.request_id)
    return (
        record is not None
        and isinstance(record.request, RetainRevision | SnapshotAndRetain)
        and getattr(record.request, "invocation", None) is None
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
    record = _record(context, observation.request_id)
    if (
        owner.closure is None
        or expected is None
        or expected.request_id != observation.request_id
        or not isinstance(expected, RetainRevision | SnapshotAndRetain)
        or record is None
        or not _request_payload_matches(expected, record.request)
        or record.observation != observation
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
        measured_input=observation.measured_input,
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
        if (refused := _refusal(owner, context, event)) is not None:
            owner = owner.model_copy(
                update={
                    "closure": refused,
                    "release_dependencies": tuple(
                        edge for edge in owner.release_dependencies if edge != event.dependency
                    ),
                }
            )
            owner, signals, requests = _progress(owner, context)
        # Discovery lists only unreleased edges, so a release that Sessions or Evaluation
        # already recorded is absent here; the proof, not the list, authorizes progress.
        elif _proof_matches(owner, context, event):
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
    target = getattr(event, "attempt", None)
    owner = next((row for row in state.attempts if _ref(row) == target), None)
    if owner is None:
        return AreaChange(state=state)
    signals: tuple[Signal, ...] = ()
    requests: tuple[Request, ...] = ()
    if isinstance(event, RetireRequested):
        owner, signals, requests = _start_closure(owner, context, event)
    elif isinstance(
        event, RetentionRequired | ReleaseDependencyObserved | ReleaseDependencyBlocked
    ):
        owner, signals, requests = _closure_event(owner, context, event)
    elif isinstance(event, WorkspaceObserved):
        owner, signals, requests = retention_acknowledged(owner, context, event)
    return AreaChange(
        state=state.model_copy(
            update={
                "attempts": tuple(owner if _ref(row) == target else row for row in state.attempts)
            }
        ),
        signals=signals,
        requests=tuple(cleanup(context.run, request) for request in requests),
    )
