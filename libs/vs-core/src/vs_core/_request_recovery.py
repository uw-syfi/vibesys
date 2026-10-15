"""Pure recovery barriers and descendant ownership, separate from the ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from . import _host_epochs
from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    Verdict,
    current_admission,
    current_closure,
    descriptor_matches,
    fresh_observation,
    invocation_for,
    observation_for,
    operation_for,
    released_owner,
    resolved_observation,
    submission_budget_for,
)
from ._registry import ContractError
from ._request_forward import turn_of
from ._session_scope import lost_turn_authority
from ._waits import owned_job_subject, recovery_waits
from .types.attempts import (
    AttemptPhase,
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    ReleaseDependencyObserved,
    RetainRevision,
    SnapshotAndRetain,
)
from .types.common import (
    AttemptId,
    AttemptRef,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    OperationNormalizationKind,
    ReleaseDependency,
    RequestId,
    RevisionAuthority,
    RunStatus,
    Scope,
)
from .types.evaluation import (
    CancelOwnedJob,
    OwnedJob,
    RegisteredOwnedJob,
    SubmitMeasurement,
)
from .types.kernel import AreaChange
from .types.records import (
    BlockRequest,
    CancelOwnedResource,
    ChildLease,
    ChildObservationWatermark,
    InspectRequest,
    Outbound,
    ReconciliationCommandLapsed,
    ReconciliationDeadline,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryCheckWaitLapsed,
    RecoveryPhase,
    RecoveryReady,
    RecoveryStarted,
    RequestObserved,
    RequestPhase,
    within,
)
from .types.scope_reopen import ScopedAdmissionReopenOutcome
from .types.sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    InvocationCancellationRequested,
    ResumeSessionTurn,
    SessionPhase,
)
from .types.until import Failed, FailureReason, WhileAlive, WhileOpen
from .types.waits import WaitKind, WaitOn, WaitSubject

if TYPE_CHECKING:
    from .types.common import Observation, ResourceId
    from .types.kernel import RecordsContext, RunState, Signal
    from .types.records import RecordsEvent, RecordsState, Request, RequestRecord, TargetObservation
    from .types.sessions import Invocation, SessionView
    from .types.strategy import Operation

type Resolution = Literal["pending", "safe-prepared", "reattached", "terminal", "blocked"]


def _child_sources(child: ChildLease, state: RecordsState) -> Verdict[tuple[Observation, ...]]:
    """Complete, exact per-source facts are the only child ownership proof."""
    if not child.watermark_history_complete:
        return Missing(ProofReason.INCOMPLETE_HISTORY)
    marks = {mark.source_request: mark.observation for mark in child.observation_watermarks}
    if set(marks) != set(child.source_requests):
        return Mismatch(ProofField.REQUEST_ID)
    for source, observation in marks.items():
        sources = state.find(source)
        if len(sources) != 1 or sources[0].request.scope != child.scope:
            return Mismatch(ProofField.REQUEST_ID)
        record = sources[0]
        if (
            not isinstance(observation_for(record, observation), Proven)
            or observation.resource_id != child.resource_id
        ):
            return Mismatch(ProofField.ADMISSION_ID)
    return Proven(tuple(marks.values()))


def _child_query(state: RecordsState, event: RequestObserved) -> Verdict[RequestRecord]:
    queries = state.find(event.observation.request_id)
    if len(queries) > 1:
        return Mismatch(ProofField.REQUEST_ID)
    if not queries:
        return Missing(ProofReason.ABSENT_REQUEST)
    query = queries[0]
    source = observation_for(query, event.observation)
    return Proven(query) if isinstance(source, Proven) else source


def _child_inspection_fact(
    state: RecordsState, event: RequestObserved
) -> Verdict[tuple[Observation, ...]]:
    """A refreshed watermark requires its exact committed successful query."""
    target = event.target
    proof = _child_query(state, event)
    if not isinstance(proof, Proven):
        return proof
    if target is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    query = proof.value
    if (
        not isinstance(query.request, InspectRequest)
        or query.request.target != target.observation.request_id
        or query.request.resource_id != target.target_resource
        or query.request.scope != target.observation.scope
        or query.request.admission_id != target.observation.admission_id
        or query.observation != event.observation
    ):
        return Mismatch(ProofField.PAYLOAD)
    if (
        query.phase != RequestPhase.COMPLETED
        or not event.observation.terminal
        or not event.observation.accepted
        or event.observation.status != ObservationStatus.SUCCEEDED
    ):
        return Missing(ProofReason.ABSENT_OBSERVATION)
    lease = next(
        (
            child
            for child in state.children
            if child.resource_id == target.target_resource
            and child.scope == target.observation.scope
        ),
        None,
    )
    source = state.record(target.observation.request_id)
    if (
        lease is not None
        and not lease.watermark_history_complete
        and (
            source is None
            or query.request_id
            != _child_inspection(source, lease.resource_id, state.recovery.epoch).request_id
        )
    ):
        return Missing(ProofReason.ABSENT_OBSERVATION)
    return Proven((target.observation,))


def _operation_decision(record: RequestRecord, context: RecordsContext) -> Operation | None:
    request = record.request
    if not isinstance(request, ExecuteRegisteredOperation):
        return None
    proof = operation_for(context.run.receipts, request)
    return proof.value if isinstance(proof, Proven) else None


def _invocation_owner(
    record: RequestRecord, context: RecordsContext, invocation: Invocation, observation: Observation
) -> bool:
    request = record.request
    registered = None
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        turn = request.turn
    elif isinstance(request, ExecuteRegisteredOperation):
        decision = _operation_decision(record, context)
        if (
            decision is None
            or decision.registered_turn is None
            or request.operation.schema_ref.lifecycle != LifecycleClass.SESSION_TURN
            or record.lifecycle != LifecycleClass.SESSION_TURN
        ):
            return False
        turn = decision.registered_turn
        registered = request.operation_id
    else:
        return False
    proof = invocation_for(context.sessions.invocations, turn, observation.scope)
    previous = invocation.observation
    return (
        isinstance(proof, Proven)
        and proof.value == invocation
        and (registered is None or invocation.registered_operation == registered)
        and (
            previous is None
            or (
                isinstance(observation_for(record, previous), Proven)
                and previous.resource_id in (None, observation.resource_id)
            )
        )
    )


def _typed_job_owner(
    record: RequestRecord,
    context: RecordsContext,
    observation: Observation,
    job: OwnedJob | RegisteredOwnedJob,
) -> bool:
    """One canonical job correspondence for reattachment and lease transfer."""
    request = record.request
    if (
        not isinstance(observation_for(record, observation), Proven)
        or job.scope != observation.scope
        or job.resource_id not in (None, observation.resource_id)
        or (
            job.observation is not None
            and (
                not isinstance(observation_for(record, job.observation), Proven)
                or job.observation.resource_id not in (None, observation.resource_id)
            )
        )
    ):
        return False
    if isinstance(job, OwnedJob):
        return (
            isinstance(request, SubmitMeasurement)
            and job.submission_id == record.request_id
            and job.plan == request.plan
        )
    if not isinstance(request, ExecuteRegisteredOperation):
        return False
    decision = _operation_decision(record, context)
    descriptor = descriptor_matches(
        context.registry,
        context.run.capabilities,
        request.operation,
        LifecycleClass.OWNED_JOB,
        OperationNormalizationKind.NONE,
    )
    return (
        decision is not None
        and isinstance(descriptor, Proven)
        and descriptor.value.resource_pool == job.resource_pool
        and job.request_id == record.request_id
        and job.operation_id == request.operation_id
        and job.expected_measurement == decision.registered_measurement
    )


def _submission_owner(record: RequestRecord, context: RecordsContext) -> bool:
    if not isinstance(record.request, SubmitMeasurement):
        return False
    return isinstance(
        submission_budget_for(
            record.request, context.evaluation.submission_budgets, context.run.receipts
        ),
        Proven,
    )


def _workspace_owner(
    record: RequestRecord, context: RecordsContext, observation: Observation
) -> bool:
    request = record.request
    if not isinstance(request, EnsureWorkspace):
        return False
    # The attempt keeps the ensure request pending only while it acquires. Once it
    # consumed the accepted answer (it left ACQUIRING) the workspace is the attempt's own,
    # and a restart must still recognise the resource as owned: otherwise the recovery
    # check of a completed ensure never resolves.
    consumed = (
        record.phase == RequestPhase.COMPLETED
        and observation.accepted
        and observation.status == ObservationStatus.SUCCEEDED
    )
    return any(
        owner.attempt_id == request.attempt.attempt_id
        and owner.generation == request.attempt.generation
        and isinstance(current_admission(owner, observation.scope, request.admission_id), Proven)
        and owner.workspace == request.plan
        and (
            record.request_id in owner.pending_records
            or (consumed and owner.phase != AttemptPhase.ACQUIRING)
        )
        for owner in context.attempts.attempts
    )


def _session_created(record: RequestRecord, observation: Observation, session: SessionView) -> bool:
    """The session is the one this completed ensure created, and it is busy with its first turn.

    A session's own acceptance waits for that turn's reply, and the turn waits for recovery
    to be ready, so recovery cannot wait for acceptance to recognise the ensure's resource.
    An idle session has no turn to wait on, so it needs its acceptance as before.
    """
    return (
        record.phase == RequestPhase.COMPLETED
        and observation.accepted
        and observation.status == ObservationStatus.SUCCEEDED
        and session.phase in (SessionPhase.ACQUIRING, SessionPhase.EXECUTING)
        and session.resource_id is not None
        and observation.resource_id == session.resource_id
    )


def _known_owner(record: RequestRecord, context: RecordsContext, observation: Observation) -> bool:
    if any(
        _typed_job_owner(record, context, observation, job)
        for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
    ):
        return True
    if _submission_owner(record, context) or _workspace_owner(record, context, observation):
        return True
    if isinstance(record.request, EnsureSession) and any(
        session.scope == observation.scope
        and session.resource_id in (None, observation.resource_id)
        and session.spec == record.request.spec
        and session.generation == observation.scope.generation
        and (
            record.request.required_resource is None
            or observation.resource_id == record.request.required_resource
        )
        and (
            (session.accepted and session.resource_id is not None)
            or record.request_id in session.pending_records
            or _session_created(record, observation, session)
        )
        for session in context.sessions.sessions
    ):
        return True
    return any(
        _invocation_owner(record, context, invocation, observation)
        for invocation in context.sessions.invocations
    )


def _prepared_episode(record: RequestRecord, context: RecordsContext) -> bool:
    request = record.request
    if record.lifecycle == LifecycleClass.QUERY or isinstance(
        request, CancelOwnedResource | CancelTurn | CancelOwnedJob | BlockRequest
    ):
        return True
    if not isinstance(request.scope.owner, AttemptId):
        return (
            request.scope.owner == context.run.run_id
            and request.scope.generation == context.run.generation
        )
    owner = next(
        (
            owner
            for owner in context.attempts.attempts
            if owner.attempt_id == request.scope.owner
            and owner.generation == request.scope.generation
        ),
        None,
    )
    if owner is None or not isinstance(
        current_admission(owner, request.scope, request.admission_id), Proven
    ):
        return False
    if owner.phase in (AttemptPhase.ACQUIRING, AttemptPhase.ACTIVE):
        return True
    cleanup = isinstance(
        request,
        CloseAttemptScope | CloseSession | DiscardWorkspace | RetainRevision | SnapshotAndRetain,
    )
    if isinstance(request, ExecuteRegisteredOperation):
        descriptor = descriptor_matches(
            context.registry,
            context.run.capabilities,
            request.operation,
            record.lifecycle,
            OperationNormalizationKind.NONE,
        )
        cleanup = isinstance(descriptor, Proven) and descriptor.value.revision_authority in (
            RevisionAuthority.SNAPSHOT,
            RevisionAuthority.RETAIN,
            RevisionAuthority.DISCARD,
        )
    if not cleanup:
        return False
    identities = {record.request_id}
    if isinstance(request, ExecuteRegisteredOperation):
        identities.add(request.operation_id)
    if isinstance(request, CloseSession):
        identities.add(request.session_id)
    closure = current_closure(owner, owner.closure)
    return isinstance(closure, Proven) and (
        closure.value.authority == record.request_id
        or any(edge.identity in identities for edge in owner.release_dependencies)
    )


def _reopen_resolved(record: RequestRecord, context: RecordsContext) -> bool:
    request = record.request
    if not isinstance(request, ExecuteRegisteredOperation):
        return True
    declarations = tuple(
        descriptor
        for descriptor in context.registry
        if descriptor.kind == request.operation.schema_ref.kind
    )
    if len(declarations) != 1:
        return False
    descriptor = descriptor_matches(
        context.registry,
        context.run.capabilities,
        request.operation,
        record.lifecycle,
        declarations[0].normalization,
    )
    if not isinstance(descriptor, Proven):
        return False
    decision = _operation_decision(record, context)
    if decision is None:
        return False
    if descriptor.value.normalization != OperationNormalizationKind.SCOPE_REOPEN:
        return True
    outcome = record.outcome
    normalization = decision.normalized_scope_reopen if decision is not None else None
    return (
        isinstance(outcome, ScopedAdmissionReopenOutcome)
        and outcome.admission != "unknown"
        and normalization is not None
        and outcome.scope
        == Scope(
            owner=normalization.attempt.attempt_id, generation=normalization.attempt.generation
        )
    )


def _session_resources(record: RequestRecord, context: RecordsContext) -> set[ResourceId]:
    request = record.request
    if isinstance(request, EnsureSession):
        session_id = request.spec.session_id
    elif isinstance(request, DispatchTurn | ResumeSessionTurn):
        session_id = request.turn.session.session_id
    elif isinstance(request, CloseSession):
        session_id = request.session_id
    else:
        return set()
    return {
        session.resource_id
        for session in context.sessions.sessions
        if session.spec.session_id == session_id
        and session.scope == request.scope
        and session.generation == request.scope.generation
        and record.request_id in session.pending_records
        and session.resource_id is not None
    }


def _owns_resource(record: RequestRecord) -> bool:
    """Whether the request creates or ends the lifecycle of the resource it reports.

    Every other request only names a resource that another request owns (a snapshot, a
    retain or an adoption names the workspace), so its observation says nothing about
    that resource's release.
    """
    return record.lifecycle in (
        LifecycleClass.OWNED_JOB,
        LifecycleClass.SESSION_TURN,
    ) or isinstance(record.request, EnsureSession | EnsureWorkspace | CloseSession)


def _resource_identified(
    record: RequestRecord, context: RecordsContext, observation: Observation
) -> bool:
    request = record.request
    if not _owns_resource(record):
        return True
    if observation.resource_id is None:
        return not observation.accepted and observation.status in (
            ObservationStatus.REJECTED,
            ObservationStatus.FAILED,
            ObservationStatus.CANCELLED,
        )
    expected = _session_resources(record, context)
    if isinstance(request, EnsureSession) and request.required_resource is not None:
        expected.add(request.required_resource)
    expected.update(
        invocation.observation.resource_id
        for invocation in context.sessions.invocations
        if invocation.observation is not None
        and invocation.observation.request_id == record.request_id
        and invocation.observation.scope == request.scope
        and invocation.observation.admission_id == request.admission_id
        and invocation.observation.resource_id is not None
    )
    return not expected or expected == {observation.resource_id}


def _resolution(record: RequestRecord, context: RecordsContext) -> Resolution:
    observation = record.observation
    if record.phase == RequestPhase.PREPARED and observation is None:
        return "safe-prepared" if _prepared_episode(record, context) else "pending"
    if (
        not isinstance(observation_for(record, observation), Proven)
        or observation is None
        or observation.status == ObservationStatus.UNKNOWN
        or not _reopen_resolved(record, context)
    ):
        return "pending"
    if not _resource_identified(record, context, observation):
        return "pending"
    terminal = observation.terminal and observation.status != ObservationStatus.PENDING
    # A completed request that does not own its resource has nothing left to recover: it
    # creates no children and its resource is another request's to release.
    names_only = (
        not _owns_resource(record)
        and observation.accepted
        and observation.status == ObservationStatus.SUCCEEDED
    )
    # A rejection that accepted nothing performed nothing: no resource, no children to find.
    refused = (
        observation.status == ObservationStatus.REJECTED
        and not observation.accepted
        and observation.resource_id is None
        and not observation.children
    )
    resource_free = (
        record.lifecycle not in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
        and (observation.resource_id is None or names_only)
        and not observation.children
        and (observation.children_complete or names_only)
    )
    if terminal and (
        record.lifecycle == LifecycleClass.QUERY
        or resource_free
        or refused
        or isinstance(released_owner(record, (record,)), Proven)
    ):
        return "terminal"
    if (
        observation.accepted
        and observation.resource_id is not None
        and observation.children_complete
        and _known_owner(record, context, observation)
    ):
        return "reattached"
    return "pending"


def _reports(record: RequestRecord, target: TargetObservation) -> bool:
    """Whether the ledger committed this inspection's facts about the record.

    A request proven never begun commits no observation: the ledger reissues it, so it is
    PREPARED again and the inspection has answered its check.
    """
    if target.reissue is not None and record.phase == RequestPhase.PREPARED:
        return True
    return record.observation == target.observation


def _inspection(record: RequestRecord, epoch: int) -> InspectRequest:
    return InspectRequest(
        request_id=RequestId(root=f"recovery:{epoch}:{record.request_id.root}"),
        scope=record.request.scope,
        admission_id=record.request.admission_id,
        target=record.request_id,
    )


def _probe(request: Request, context: RecordsContext) -> Outbound:
    """A reconciliation request goes out with the run's reconciliation bound."""
    return within(request, context.run.limits.reconciliation_bound)


def _child_inspection(record: RequestRecord, resource: ResourceId, epoch: int) -> InspectRequest:
    return _inspection(record, epoch).model_copy(
        update={
            "request_id": RequestId(
                root=f"recovery:child:{epoch}:{len(record.request_id.root)}:{record.request_id.root}:{resource.root}"
            ),
            "resource_id": resource,
        }
    )


def _child_released(child: ChildLease, state: RecordsState) -> bool:
    return isinstance(released_owner(child, state.records), Proven)


def _child_ready(child: ChildLease) -> bool:
    observation = child.observation
    return (
        observation is not None
        and observation.request_id in child.source_requests
        and observation.scope == child.scope
        and observation.resource_id == child.resource_id
        and (
            isinstance(resolved_observation(observation), Proven)
            or (
                observation is not None
                and observation.status != ObservationStatus.UNKNOWN
                and observation.accepted
                and observation.children_complete
            )
        )
    )


def _child_proven(child: ChildLease, state: RecordsState) -> bool:
    proof = _child_sources(child, state)
    return isinstance(proof, Proven) and all(
        _child_ready(child.model_copy(update={"observation": row})) for row in proof.value
    )


def _validate_children(state: RecordsState) -> None:
    for index, child in enumerate(state.children):
        admissions = set()
        for source in child.source_requests:
            record = state.record(source)
            if record is None or record.request.scope != child.scope:
                raise ContractError(
                    ("children", index, "source_requests", source.root),
                    "child requires a canonical source in its exact scope",
                )
            admissions.add(record.request.admission_id)
        if len(admissions) > 1:
            raise ContractError(
                ("children", index, "source_requests"),
                "conflicting child ownership admission episodes require distinct resource leases",
            )


def _transfer_children(state: RecordsState, context: RecordsContext) -> RecordsState:
    _validate_children(state)
    retained = []
    for child in state.children:
        # A provisional single-source discovery can attach to its exact typed
        # owner below. Retained observations and independent source claims
        # cannot be transferred without their complete history proof.
        if (child.observation is not None or len(child.source_requests) > 1) and not _child_proven(
            child, state
        ):
            retained.append(child)
            continue
        if len(child.source_requests) > 1 and not _child_released(child, state):
            retained.append(child)
            continue
        if any(
            record.request_id in child.source_requests
            and record.phase == RequestPhase.PREPARED
            and record.observation is None
            for record in state.records
        ):
            retained.append(child)
            continue
        matches = [
            job
            for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
            if job.resource_id == child.resource_id
        ]
        if any(job.scope != child.scope for job in matches):
            raise ContractError(
                ("children", "scope"), "typed owner conflicts with discovered child scope"
            )
        transferred = False
        for job in matches:
            request_id = (
                job.request_id if isinstance(job, RegisteredOwnedJob) else job.submission_id
            )
            source = state.record(request_id)
            ancestor = state.record(child.source_requests[0])
            if ancestor is None:
                raise ContractError(("children",), "child requires a canonical source")
            observation = job.observation
            if (
                source is not None
                and source.request.admission_id == ancestor.request.admission_id
                and (child.observation is None or child.observation.request_id == source.request_id)
                and observation is not None
                and _typed_job_owner(source, context, observation, job)
                and (
                    source.request.scope == child.scope
                    and observation.request_id == request_id
                    and observation.scope == child.scope
                    and observation.resource_id == child.resource_id
                    and observation.admission_id == source.request.admission_id
                    and (
                        isinstance(resolved_observation(observation), Proven)
                        or (
                            observation.accepted
                            and observation.children_complete
                            and observation.status != ObservationStatus.UNKNOWN
                        )
                    )
                )
            ):
                transferred = True
                break

        if not transferred:
            retained.append(child)
    return state.model_copy(update={"children": tuple(retained)})


def _with_resolution(check: RecoveryCheck, resolution: Resolution) -> RecoveryCheck:
    """The check after a new observation resolved it, never moving a blocked check to pending.

    The reconciliation deadline blocks a check once it has ended the target and issued one last
    inspection. That inspection can answer unknown again, which resolves to pending, and nothing
    would ever answer a pending check whose target already ended. Blocked stays blocked.
    """
    if check.resolution == "blocked" and resolution == "pending":
        return check
    return check.model_copy(update={"resolution": resolution})


def _aggregate_resolution(
    record: RequestRecord, context: RecordsContext, state: RecordsState
) -> Resolution:
    resolution = _resolution(record, context)
    if resolution == "safe-prepared" and any(
        record.request_id in child.source_requests for child in state.children
    ):
        return "pending"
    if any(
        record.request_id in child.source_requests and not _child_proven(child, state)
        for child in state.children
    ):
        return "pending"
    return resolution


#: The phases of a record whose answer is still to come, a reconciliation command's included.
_IN_FLIGHT = (RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING)


def _check_wait(
    state: RecordsState,
    context: RecordsContext,
    check: RecoveryCheck,
    sent: frozenset[RequestId],
    epoch: int,
) -> WaitOn | None:
    """What a pending check waits on now: the requests and jobs whose answer re-resolves it.

    Any committed answer about the target re-resolves the check (``_observe``): the target
    itself while it is open, and every inspection of it, the check's own, an earlier epoch's
    sent again, and the inspections of the children it left. A child its typed job ledger
    tracks settles when that job does, so the check names the job, but only when every child
    has one: a child nobody answers for keeps the check from ever resolving. *sent* holds the
    requests this transition sends. A check left with nothing keeps the wait it had, naming
    what it waited on, so the run halts with it named.
    """
    if check.resolution != "pending":
        return None
    record = state.record(check.target)
    children = [child for child in state.children if check.target in child.source_requests]
    expected = [check.target, check.inspection]
    if record is not None:
        expected += [
            _child_inspection(record, child.resource_id, epoch).request_id for child in children
        ]
    expected += [
        row.request_id
        for row in state.records
        if isinstance(row.request, InspectRequest) and row.request.target == check.target
    ]
    requests = tuple(
        dict.fromkeys(
            request
            for request in expected
            if request is not None
            and (
                request in sent
                or ((row := state.record(request)) is not None and row.phase in _IN_FLIGHT)
            )
        )
    )
    jobs = [
        next(
            (
                job
                for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
                if job.wait is not None
                and job.resource_id == child.resource_id
                and job.scope == child.scope
            ),
            None,
        )
        for child in children
    ]
    subjects = (
        tuple(dict.fromkeys(owned_job_subject(job) for job in jobs if job is not None))
        if children and None not in jobs
        else ()
    ) + _owner_sessions(record, context)
    if not requests and not subjects:
        return check.wait
    return WaitOn(requests=requests, subjects=subjects)


def _owner_sessions(
    record: RequestRecord | None, context: RecordsContext
) -> tuple[WaitSubject, ...]:
    """The waiting sessions an ensure's check waits on to learn whether they own its resource.

    An ensure resolves as reattached once its session is known to hold the resource it
    reported (``_known_owner``). A session still settling its own state (unknown after a
    crash, inspecting a turn) moves the check when it settles, so the check names it.
    """
    if record is None or not isinstance(record.request, EnsureSession):
        return ()
    request = record.request
    return tuple(
        WaitSubject(kind=WaitKind.SESSION, identity=session.spec.session_id.root)
        for session in context.sessions.sessions
        if session.wait is not None
        and session.scope == request.scope
        and session.spec == request.spec
    )


def _recheck(
    state: RecordsState, context: RecordsContext, event: RecoveryCheckWaitLapsed
) -> AreaChange[RecordsState]:
    """Resolve a check again once nothing it named can move it, and name what it waits on now.

    Its own inspection answered before what it waited on settled (an ensure inspected while
    its session was still unknown), so the answer resolved nothing then. The target's
    resolution reads the current state, so a check whose owner became known resolves now.
    """
    barrier = state.recovery
    if not recovery_waits(barrier.phase):
        return AreaChange(state=state)
    checks = tuple(
        _with_resolution(check, _aggregate_resolution(record, context, state))
        if check.target == event.target
        and check.resolution == "pending"
        and (record := state.record(check.target)) is not None
        else check
        for check in barrier.checks
    )
    return _finish(state, barrier.model_copy(update={"checks": checks}), context)


def _finish(
    state: RecordsState,
    barrier: RecoveryBarrier,
    context: RecordsContext,
    sent: tuple[Outbound, ...] = (),
) -> AreaChange[RecordsState]:
    """*barrier* stored with its phase from its checks, each pending one naming its wait."""
    sent_ids = frozenset(
        outbound.request.request_id for outbound in sent if outbound.request.request_id is not None
    )
    barrier = barrier.model_copy(
        update={
            "checks": tuple(
                check.model_copy(
                    update={"wait": _check_wait(state, context, check, sent_ids, barrier.epoch)}
                )
                for check in barrier.checks
            )
        }
    )
    ready = all(check.resolution not in ("pending", "blocked") for check in barrier.checks)
    phase = (
        RecoveryPhase.READY
        if ready
        else RecoveryPhase.BLOCKED
        if any(check.resolution == "blocked" for check in barrier.checks)
        else RecoveryPhase.RECOVERING
    )
    barrier = barrier.model_copy(update={"phase": phase})
    signals = (
        (RecoveryReady(epoch=barrier.epoch),)
        if phase == RecoveryPhase.READY
        and (state.recovery.phase != RecoveryPhase.READY or state.recovery.epoch != barrier.epoch)
        else ()
    )
    return AreaChange(state=state.model_copy(update={"recovery": barrier}), signals=signals)


def _restart_command(
    state: RecordsState, context: RecordsContext, event: RecoveryStarted, record: RequestRecord
) -> tuple[RecordsState, tuple[Outbound, ...]]:
    """What a new epoch does with an earlier epoch's open inspection, cancel or block.

    A cancel or block the dead host dispatched is inspected, and lives until the answer with
    a fresh bound from the restart, like the inspection itself: kept at the dead host's
    bound, a restart after it would lapse the command on the first tick, before the
    inspection's "never began" proof could send it again.
    """
    if isinstance(record.request, InspectRequest) or record.phase == RequestPhase.PREPARED:
        return _requeue_lost_inspection(state, context, event, record), ()
    if record.phase in (RequestPhase.DISPATCHED, RequestPhase.RECONCILING):
        bound = _fresh_bound(context, event)
        kept = record.model_copy(
            update={"until": record.until.model_copy(update={"at": max(record.until.at, bound)})}
        )
        state = state.model_copy(
            update={
                "records": tuple(
                    kept if row.request_id == record.request_id else row for row in state.records
                )
            }
        )
        return state, (_probe(_inspection(kept, event.epoch), context),)
    return state, ()


def _fresh_bound(context: RecordsContext, event: RecoveryStarted) -> float:
    """The reconciliation bound a command of the epoch ``event`` opens gets from its start."""
    return max(context.run.now_at, event.now_at) + context.run.limits.reconciliation_bound


def _requeue_lost_inspection(
    state: RecordsState, context: RecordsContext, event: RecoveryStarted, record: RequestRecord
) -> RecordsState:
    """An earlier epoch's inspection that never reported, or command never sent, goes out.

    Its target is inspected afresh by this epoch, but the old request would stay open until
    its bound and hold its waiters on nothing. An inspection only reads, so issuing it again
    is safe; a cancel or a block still PREPARED never went out, so sending it is its first
    send. A cancel or a block a dead host dispatched is not sent again: it closes only by
    an inspection of it (``_start``), whose proof decides a re-send. It goes out with a
    fresh reconciliation bound from the restart, as this epoch's own inspections do: one
    issued with a bound that passed while the host was down would lapse unanswered.
    """
    open_phases = (
        (RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING)
        if isinstance(record.request, InspectRequest)
        else (RequestPhase.PREPARED,)
    )
    if not (record.phase in open_phases and record.observation is None):
        return state
    bound = _fresh_bound(context, event)
    requeued = record.model_copy(
        update={
            "phase": RequestPhase.PREPARED,
            "until": record.until.model_copy(update={"at": max(record.until.at, bound)}),
        }
    )
    return state.model_copy(
        update={
            "records": tuple(
                requeued if row.request_id == record.request_id else row for row in state.records
            )
        }
    )


def _opens_epoch(barrier: RecoveryBarrier, event: RecoveryStarted) -> bool:
    """Whether ``event`` starts a recovery not yet started (a repeat of one changes nothing)."""
    return event.epoch > barrier.epoch or (
        event.epoch == barrier.epoch and barrier.phase == RecoveryPhase.REQUIRED
    )


def open_host_epoch(state: RecordsState, run: RunState, event: RecoveryStarted) -> RecordsState:
    """Record the epoch ``event`` opens, and move every open record's limit past the downtime.

    A limit says how long core waits for the far side, and while no host runs nothing waits:
    a request prepared but never sent, or sent and never begun, would otherwise end at the
    restart without ever having had its time. The kernel calls this before ``event`` moves
    the clock, while ``run.now_at`` is still the dead host's last commit, which proves it
    alive as its lease renewals do. The credit is ``downtime`` of the host log, so it never
    exceeds the real downtime by more than one renewal interval. The run deadline caps the
    moved limit as ``resolve_until`` caps a new one; a limit already past the deadline (core's
    own cleanup budget) keeps its full length. A repeated or stale start changes nothing.
    """
    if not _opens_epoch(state.recovery, event):
        return state
    hosts = _host_epochs.opened(state.hosts, event.epoch, event.now_at, run.now_at)
    state = state.model_copy(update={"hosts": hosts})
    credit = _host_epochs.downtime(hosts)
    if credit <= 0 or run.status == RunStatus.TERMINAL:
        return state

    def moved(record: RequestRecord) -> RequestRecord:
        at = record.until.at + credit
        if record.until.at <= run.deadline_at:
            at = min(at, run.deadline_at)
        if not is_open(record) or at == record.until.at:
            return record
        return record.model_copy(update={"until": record.until.model_copy(update={"at": at})})

    records = tuple(moved(record) for record in state.records)
    if records == state.records:
        return state
    return state.model_copy(update={"records": records})


def _start(
    state: RecordsState, context: RecordsContext, event: RecoveryStarted
) -> AreaChange[RecordsState]:
    barrier = state.recovery
    _validate_children(state)
    if not _opens_epoch(barrier, event):
        return AreaChange(state=state)
    if context.run.status == RunStatus.TERMINAL:
        # The run ended, so its closure already proved every owner released: there is nothing
        # left to inspect, and a terminal run may issue no request. A record core itself ended
        # (one blocked when the run closed) has no executor fact that would resolve it.
        return _finish(
            state, RecoveryBarrier(epoch=event.epoch, phase=RecoveryPhase.RECOVERING), context
        )
    updated = state
    for record in state.records:
        proof = observation_for(record, record.observation)
        if isinstance(proof, Proven):
            updated = _observe_children(updated, RequestObserved(observation=proof.value))
    checks: list[RecoveryCheck] = []
    requests: list[Outbound] = []
    targets = {record.request_id for record in state.records}
    _validate_children(updated)
    updated = _transfer_children(updated, context)
    for record in state.records:
        if (
            isinstance(record.request, InspectRequest | CancelOwnedResource | BlockRequest)
            and record.request.target in targets
            and not any(
                record.request_id in child.source_requests and not _child_proven(child, updated)
                for child in updated.children
            )
        ):
            updated, sent = _restart_command(updated, context, event, record)
            requests.extend(sent)
            continue
        resolution = _aggregate_resolution(record, context, updated)
        inspection = (
            _inspection(record, event.epoch)
            if resolution == "pending"
            and _resolution(record, context) not in ("terminal", "reattached")
            else None
        )
        if resolution == "pending" and _blocked_before(barrier, record):
            # The dead host's recovery already blocked on this record; a restart learns
            # nothing new by itself, so recovery stays blocked until an answer settles it.
            resolution = "blocked"
        checks.append(
            RecoveryCheck(
                target=record.request_id,
                inspection=inspection.request_id if inspection is not None else None,
                resolution=resolution,
                wait=None,
            )
        )
        if inspection is not None:
            requests.append(_probe(inspection, context))
        requests.extend(
            _probe(_child_inspection(record, child.resource_id, event.epoch), context)
            for child in updated.children
            if record.request_id in child.source_requests and not _child_proven(child, updated)
        )
    change = _finish(
        updated,
        RecoveryBarrier(epoch=event.epoch, phase=RecoveryPhase.RECOVERING, checks=tuple(checks)),
        context,
        tuple(requests),
    )
    return change.model_copy(update={"requests": tuple(requests)})


def _blocked_before(barrier: RecoveryBarrier, record: RequestRecord) -> bool:
    """Whether the previous epoch's recovery blocked on ``record`` (see ``_deadline_barrier``)."""
    return any(
        check.target == record.request_id and check.resolution == "blocked"
        for check in barrier.checks
    )


def _merge_child(
    children: list[ChildLease], observation: Observation, resource: ResourceId
) -> None:
    if resource == observation.resource_id:
        raise ContractError(("children",), "resource cannot be its own descendant")
    previous = next((child for child in children if child.resource_id == resource), None)
    parents = (observation.resource_id,) if observation.resource_id is not None else ()
    ancestors = set(parents)
    frontier = list(parents)
    while frontier:
        ancestor = frontier.pop()
        lease = next((child for child in children if child.resource_id == ancestor), None)
        if lease is not None:
            for parent in lease.parent_resources:
                if parent not in ancestors:
                    ancestors.add(parent)
                    frontier.append(parent)
    if resource in ancestors:
        raise ContractError(("children", "parent_resources"), "cyclic child ownership ancestry")
    if previous is not None:
        if previous.scope != observation.scope:
            raise ContractError(("children", "scope"), "conflicting child ownership scope")
        parents = tuple(
            sorted({*previous.parent_resources, *parents}, key=lambda value: value.root)
        )
        sources = tuple(
            sorted(
                {*previous.source_requests, observation.request_id},
                key=lambda value: value.root,
            )
        )
        updated = previous.model_copy(
            update={
                "source_requests": sources,
                "parent_resources": parents,
                "watermark_history_complete": previous.watermark_history_complete
                and sources == previous.source_requests,
            }
        )
        children[children.index(previous)] = updated
    else:
        children.append(
            ChildLease(
                resource_id=resource,
                scope=observation.scope,
                source_requests=(observation.request_id,),
                parent_resources=parents,
            )
        )


def _source_observation(state: RecordsState, event: RequestObserved) -> Observation:
    observation = event.target.observation if event.target is not None else event.observation
    sources = state.find(observation.request_id)
    if len(sources) != 1:
        raise ContractError(
            ("observation", "request_id"), "child ownership requires a unique canonical source"
        )
    source = sources[0]
    if not isinstance(observation_for(source, observation), Proven):
        raise ContractError(
            ("observation", "admission_id"), "child ownership requires canonical source episode"
        )
    return observation


def _ignore_ownership_fact(
    canonical: Observation | None, observation: Observation, *, child: bool
) -> bool:
    """Compare sequence and equality only within one retained source."""
    if canonical is None:
        return False
    if canonical.request_id != observation.request_id:
        return child
    proof = fresh_observation((canonical,), observation, complete=True)
    if not isinstance(proof, Proven):
        if observation.sequence < canonical.sequence:
            return True
        raise ContractError(
            ("observation", "sequence"), "conflicting ownership observation sequence"
        )
    return child and observation == canonical


def _observe_fact(state: RecordsState, event: RequestObserved) -> RecordsState:
    children = list(state.children)
    observation = _source_observation(state, event)
    previous = None
    if event.target is not None and event.target.target_resource is not None:
        inspection = _child_inspection_fact(state, event)
        if isinstance(inspection, Missing):
            return state
        if isinstance(inspection, Mismatch):
            raise ContractError(
                ("target", inspection.field),
                "child inspection differs from committed canonical query",
            )
        previous = next(
            (child for child in children if child.resource_id == event.target.target_resource), None
        )
        if (
            previous is None
            or previous.scope != observation.scope
            or observation.request_id not in previous.source_requests
        ):
            raise ContractError(
                ("target", "resource_id"), "child observation requires exact ownership proof"
            )
        mark = next(
            (
                mark
                for mark in previous.observation_watermarks
                if mark.source_request == observation.request_id
            ),
            None,
        )
        canonical = (
            mark.observation
            if mark is not None
            else (
                previous.observation
                if previous.observation is not None
                and previous.observation.request_id == observation.request_id
                else None
            )
        )
    else:
        record = state.record(observation.request_id)
        if record is None or observation.scope != record.request.scope:
            raise ContractError(("observation",), "unknown canonical ownership source")
        canonical = record.observation
    if _ignore_ownership_fact(
        canonical,
        observation,
        child=previous is not None and mark is not None and previous.watermark_history_complete,
    ):
        return state
    for resource in observation.children:
        _merge_child(children, observation, resource)
    if previous is not None:
        marks = {mark.source_request: mark for mark in previous.observation_watermarks}
        marks[observation.request_id] = ChildObservationWatermark(
            source_request=observation.request_id, observation=observation
        )
        # Migration retains one historical source bound outside the certified
        # manifest. Another source cannot erase it before that source refreshes.
        aggregate = (
            previous.observation
            if previous.observation is not None and previous.observation.request_id not in marks
            else observation
        )
        children[children.index(previous)] = previous.model_copy(
            update={
                "observation": aggregate,
                "observation_watermarks": tuple(
                    marks[source] for source in sorted(marks, key=lambda source: source.root)
                ),
                "watermark_history_complete": set(marks) == set(previous.source_requests),
            }
        )
    return state.model_copy(
        update={"children": tuple(sorted(children, key=lambda child: child.resource_id.root))}
    )


def _observe_children(state: RecordsState, event: RequestObserved) -> RecordsState:
    if event.target is not None:
        state = _observe_fact(state, event.model_copy(update={"target": None}))
    updated = _observe_fact(state, event)
    _validate_children(updated)
    return updated


def _child_release_signals(
    before: RecordsState, after: RecordsState, context: RecordsContext
) -> tuple[ReleaseDependencyObserved, ...]:
    signals = []
    for child in after.children:
        previous = next(
            (lease for lease in before.children if lease.resource_id == child.resource_id), None
        )
        if (
            child.observation is None
            or child.observation.request_id not in child.source_requests
            or not _child_released(child, after)
            or (previous is not None and _child_released(previous, before))
        ):
            continue
        observation = child.observation
        if observation is None or not isinstance(child.scope.owner, AttemptId):
            continue
        dependency = ReleaseDependency(kind="job", identity=child.resource_id)
        if any(
            owner.attempt_id == child.scope.owner
            and owner.generation == child.scope.generation
            and dependency in owner.release_dependencies
            for owner in context.attempts.attempts
        ):
            signals.append(
                ReleaseDependencyObserved(
                    attempt=AttemptRef(
                        attempt_id=child.scope.owner, generation=child.scope.generation
                    ),
                    dependency=dependency,
                    observation=observation,
                )
            )
    return tuple(signals)


def _inspection_exists(state: RecordsState, inspection: InspectRequest) -> bool:
    previous = state.record(inspection.request_id)
    if previous is None:
        return False
    if not isinstance(previous.request, InspectRequest) or any(
        getattr(previous.request, field) != getattr(inspection, field)
        for field in ("scope", "admission_id", "target", "resource_id")
    ):
        raise ContractError(("request_id",), "inspection successor identity conflict")
    return True


def _pending_probes(
    state: RecordsState, context: RecordsContext, barrier: RecoveryBarrier
) -> tuple[tuple[RecoveryCheck, ...], tuple[Outbound, ...]]:
    checks = list(barrier.checks)
    requests = []
    for record in state.records:
        children = tuple(
            child
            for child in state.children
            if record.request_id in child.source_requests and not _child_proven(child, state)
        )
        if not children:
            continue
        check = next((check for check in checks if check.target == record.request_id), None)
        if check is None:
            inspection = (
                _inspection(record, barrier.epoch)
                if _resolution(record, context) not in ("terminal", "reattached")
                else None
            )
            checks.append(
                RecoveryCheck(
                    target=record.request_id,
                    inspection=inspection.request_id if inspection is not None else None,
                    wait=None,
                )
            )
            if inspection is not None and not _inspection_exists(state, inspection):
                requests.append(_probe(inspection, context))
        elif check.resolution not in ("pending", "blocked"):
            inspection = (
                _inspection(record, barrier.epoch)
                if _resolution(record, context) not in ("terminal", "reattached")
                else None
            )
            replacement = check.model_copy(
                update={
                    "resolution": "pending",
                    "inspection": check.inspection
                    or (inspection.request_id if inspection is not None else None),
                }
            )
            checks[checks.index(check)] = replacement
            if (
                check.inspection is None
                and inspection is not None
                and not _inspection_exists(state, inspection)
            ):
                requests.append(_probe(inspection, context))
        for child in children:
            inspection = _child_inspection(record, child.resource_id, barrier.epoch)
            if not _inspection_exists(state, inspection):
                requests.append(_probe(inspection, context))
    return tuple(checks), tuple(requests)


def _sourced(state: RecordsState, target: RequestId) -> bool:
    return any(target in child.source_requests for child in state.children)


def _observe(
    state: RecordsState, context: RecordsContext, event: RequestObserved
) -> AreaChange[RecordsState]:
    observed = _observe_children(state, event)
    release_signals = _child_release_signals(state, observed, context)
    updated = _transfer_children(observed, context)
    barrier = state.recovery
    if barrier.phase == RecoveryPhase.REQUIRED:
        return AreaChange(state=updated, signals=release_signals)
    pending_checks, requests = _pending_probes(updated, context, barrier)
    if barrier.phase == RecoveryPhase.READY and pending_checks == barrier.checks:
        return AreaChange(state=updated, signals=release_signals)
    target = (
        event.target.observation.request_id
        if event.target is not None
        else event.observation.request_id
    )
    checks = []
    for check in pending_checks:
        # An old host's query can update ownership, but cannot open this epoch's gate.
        record = state.record(check.target)
        child_resource = event.target.target_resource if event.target is not None else None
        expected_child = (
            _child_inspection(record, child_resource, barrier.epoch).request_id
            if record is not None and child_resource is not None
            else None
        )
        # One fact, one path: any committed observation about the target re-resolves its
        # check from the target's current resolution, whichever inspection delivered it
        # (the check's own, a turn's InspectTurn from Sessions, or a continuation's).
        # A child's check still names its own inspection: the child is a different resource.
        matches = (
            check.target == target
            and event.target is not None
            and (
                record is not None and _reports(record, event.target)
                if child_resource is None
                else expected_child == event.observation.request_id
            )
        )
        # A child its typed job ledger took over (``_transfer_children``) is that ledger's to
        # settle, so the check that waited on it re-resolves without the child.
        handed_over = _sourced(state, check.target) and not _sourced(updated, check.target)
        resolution = (
            _aggregate_resolution(record, context, updated)
            if (matches or handed_over) and record is not None
            else check.resolution
        )
        # A record core already ended as blocked (its owner heard so) has no deadline left to
        # end its check: an answer that only repeats its unknown result settles the check as
        # the block did, keeping the record blocked and its ownership as it was.
        if (
            matches
            and resolution == "pending"
            and check.resolution == "pending"
            and record is not None
            and record.phase == RequestPhase.BLOCKED
            and child_resource is None
            and not _sourced(updated, check.target)
        ):
            resolution = "terminal"
        checks.append(_with_resolution(check, resolution))
    change = _finish(
        updated, barrier.model_copy(update={"checks": tuple(checks)}), context, requests
    )
    return change.model_copy(
        update={"signals": (*release_signals, *change.signals), "requests": requests}
    )


def _deadline_barrier(
    state: RecordsState, context: RecordsContext, record: RequestRecord
) -> tuple[RecoveryBarrier, InspectRequest | None]:
    if _aggregate_resolution(record, context, state) == "reattached":
        return state.recovery, None
    inspection = _inspection(record, state.recovery.epoch).model_copy(
        update={
            "request_id": RequestId(
                root=f"recovery:deadline:{state.recovery.epoch}:{record.request_id.root}"
            ),
        }
    )
    checks = list(state.recovery.checks)
    check = next((check for check in checks if check.target == record.request_id), None)
    replacement = RecoveryCheck(
        target=record.request_id,
        inspection=inspection.request_id,
        resolution="blocked",
        wait=None,
    )
    if check is None:
        checks.append(replacement)
    else:
        checks[checks.index(check)] = replacement
    existing = state.record(inspection.request_id)
    if existing is not None and (
        not isinstance(existing.request, InspectRequest)
        or existing.request.target != record.request_id
        or existing.request.resource_id is not None
        or existing.request.scope != record.request.scope
        or existing.request.admission_id != record.request.admission_id
    ):
        raise ContractError(("request_id",), "inspection successor identity conflict")
    barrier = state.recovery.model_copy(
        update={"phase": RecoveryPhase.BLOCKED, "checks": tuple(checks)}
    )
    return barrier, inspection if existing is None else None


def _pending_cancellations(
    state: RecordsState, cancellations: tuple[CancelOwnedResource, ...]
) -> tuple[CancelOwnedResource, ...]:
    requests = []
    for cancellation in cancellations:
        previous = state.record(cancellation.request_id)
        if previous is None:
            requests.append(cancellation)
        elif not isinstance(previous.request, CancelOwnedResource) or any(
            getattr(previous.request, field) != getattr(cancellation, field)
            for field in ("scope", "admission_id", "target", "resource_id")
        ):
            raise ContractError(("request_id",), "cancellation successor identity conflict")
    return tuple(requests)


def _deadline_target(state: RecordsState, request_id: RequestId) -> RequestRecord:
    seen = set()
    while True:
        if request_id in seen:
            raise ContractError(("request", "target"), "cyclic reconciliation command ancestry")
        seen.add(request_id)
        record = state.record(request_id)
        if record is None:
            raise ContractError(("request_id", request_id.root), "unknown reconciliation target")
        request = record.request
        if not isinstance(request, InspectRequest | CancelOwnedResource | BlockRequest):
            return record
        target = state.record(request.target)
        if target is None:
            raise ContractError(
                ("request", "target", request.target.root),
                "missing canonical reconciliation target",
            )
        if (
            request.scope != target.request.scope
            or request.admission_id != target.request.admission_id
        ):
            raise ContractError(
                ("request", "target"),
                "reconciliation command conflicts with target scope or episode",
            )
        request_id = request.target


def _awaiting_report(state: RecordsState, record: RequestRecord) -> float | None:
    """When the executor's report on ``record`` stops being waited for, or ``None``.

    A record that was in flight when its host died waits for the inspection this epoch sent
    for it: the executor may have finished it while the host was down, and a limit that passed
    meanwhile says how long the far side was waited for, not that it failed. The wait is the
    inspection's own bound, so an executor that stays silent still ends the record at its limit.
    """
    if state.recovery.phase == RecoveryPhase.READY:
        return None
    check = next((row for row in state.recovery.checks if row.target == record.request_id), None)
    if check is None or check.inspection is None or check.resolution != "pending":
        return None
    inspection = state.record(check.inspection)
    if inspection is None or inspection.observation is not None:
        return None
    return inspection.until.at


def _deadline(
    state: RecordsState, context: RecordsContext, event: ReconciliationDeadline
) -> AreaChange[RecordsState]:
    record = _deadline_target(state, event.request_id)
    now_at = max(context.run.now_at, event.now_at)
    _validate_children(state)
    unresolved_children = any(
        record.request_id in child.source_requests and not _child_released(child, state)
        for child in state.children
    )
    resolution = _resolution(record, context)
    # A passed limit always ends a record that still owes a result, whatever the executor is
    # doing (a reattached live one included): the shell cancels its flight at the same
    # instant. A record already proven terminal, or one that already concluded and has no
    # children to release, keeps its result. Ending moves the record to BLOCKED, which is
    # not open, so the deadline cannot be raised for it again. One exception: a record the
    # restarted host is still asking the executor about (see ``_awaiting_report``) waits for
    # the answer or the inspection's bound, because the limit may have passed while the host
    # was down and the far side may have finished in time.
    timed = event.reason == FailureReason.TIMED_OUT
    concluded = not is_open(record) and not unresolved_children
    asked_until = _awaiting_report(state, record)
    if (
        (timed and now_at < record.until.at)
        or (timed and asked_until is not None and now_at < asked_until)
        or (resolution == "terminal" and not unresolved_children)
        or (timed and concluded and resolution == "reattached")
    ):
        return AreaChange(state=state)
    block_id = RequestId(root=f"recovery:block:{state.recovery.epoch}:{record.request_id.root}")
    previous_block = state.record(block_id)
    if previous_block is not None:
        request = previous_block.request
        if (
            not isinstance(request, BlockRequest)
            or request.target != record.request_id
            or request.scope != record.request.scope
            or request.admission_id != record.request.admission_id
        ):
            raise ContractError(("request_id",), "reconciliation successor identity conflict")
    block = BlockRequest(
        request_id=block_id,
        scope=record.request.scope,
        admission_id=record.request.admission_id,
        target=record.request_id,
        reason=event.reason,
    )
    requests: list[Outbound] = []
    signals = _lost_turn(context, record, first=previous_block is None)
    if previous_block is None:
        requests.append(_probe(block, context))
    state, events = _end_open_record(
        state, record, event.reason, lost=bool(signals), first=previous_block is None
    )
    observation = record.observation
    resources = []
    if (
        resolution != "reattached"
        and observation is not None
        and observation.accepted
        and observation.resource_id is not None
        and isinstance(observation_for(record, observation), Proven)
        and _resource_identified(record, context, observation)
        and not isinstance(resolved_observation(observation), Proven)
        and _cancelled_here(record)
    ):
        resources.append(observation.resource_id)
    resources.extend(
        child.resource_id
        for child in state.children
        if child.scope == record.request.scope
        and record.request_id in child.source_requests
        and not _child_released(child, state)
    )
    cancellations = (
        CancelOwnedResource(
            request_id=RequestId(
                root=f"recovery:cancel:{state.recovery.epoch}:{len(record.request_id.root)}:{record.request_id.root}:{resource.root}"
            ),
            scope=record.request.scope,
            admission_id=record.request.admission_id,
            resource_id=resource,
            target=record.request_id,
        )
        for resource in sorted(set(resources), key=lambda value: value.root)
    )
    requests.extend(
        within(cancellation, context.run.limits.cancellation_bound)
        for cancellation in _pending_cancellations(state, tuple(cancellations))
    )
    barrier = state.recovery
    if barrier.phase != RecoveryPhase.READY:
        # Recovery is gating the run: record the blocked check and inspect once more. A
        # bound passing while the run is live (an executor answered and went silent) blocks
        # the record for the strategy to handle and leaves scheduling open.
        barrier, inspection = _deadline_barrier(state, context, record)
        if inspection is not None:
            requests.append(_probe(inspection, context))

    return AreaChange(
        state=state.model_copy(update={"recovery": barrier}),
        signals=signals,
        requests=tuple(requests),
        events=events,
    )


def _lapse_command(
    state: RecordsState, context: RecordsContext, event: ReconciliationCommandLapsed
) -> AreaChange[RecordsState]:
    """End an inspection, cancel or block that is still open at its limit.

    Left open, it would count as live forever for every wait that names it. The command has
    no owner to tell; its target's fate is decided by whatever waits on the command.
    """
    _deadline_target(state, event.request_id)
    command = state.record(event.request_id)
    now_at = max(context.run.now_at, event.now_at)
    if command is None or not command_open(command) or now_at < command.until.at:
        return AreaChange(state=state)
    blocked = command.model_copy(update={"phase": RequestPhase.BLOCKED})
    return AreaChange(state=_replace_record(state, blocked))


def _cancelled_here(record: RequestRecord) -> bool:
    """Whether the record's own resource is cancelled here, by ``CancelOwnedResource``.

    That command reaches the operations executor, which holds receipts for registered
    operations only: it can end an operation's resource and nothing else. Every other
    resource is released by the area that owns it, through the command whose outcome that
    area already defines, so sending a generic cancel for it only adds a command that is
    answered "unknown". Evaluation terminates an owned job (``CancelOwnedJob``) at the job's
    own deadline or the run's close, within its retry limit. A turn's session is cancelled
    through ``_lost_turn`` (``CancelTurn``). Sessions closes a session's lease when the block
    wakes the session's wait (``CloseSession``), and an attempt's retirement discards its
    workspace (``DiscardWorkspace``). A request that only names a resource another request
    owns has nothing of its own to release.
    """
    return isinstance(record.request, ExecuteRegisteredOperation) and record.lifecycle not in (
        LifecycleClass.OWNED_JOB,
        LifecycleClass.SESSION_TURN,
    )


def _end_open_record(
    state: RecordsState, record: RequestRecord, reason: FailureReason, *, lost: bool, first: bool
) -> tuple[RecordsState, tuple[Failed, ...]]:
    """Block a record whose result is still owed, and the failure its owner hears of it.

    One that already concluded (it is here for its unreleased children) has told its owner
    already. A lost turn is answered by its session, once it is cancelled: the ask hears that,
    not a failure of the record (which would discard that answer as late).
    """
    if not is_open(record):
        return state, ()
    failed = None if lost else record.failure or _failure(record, reason)
    heard = (failed,) if failed is not None and first and not _decision_ended(state, record) else ()
    blocked = record.model_copy(update={"phase": RequestPhase.BLOCKED, "failure": failed})
    return _replace_record(state, blocked), heard


def _lost_turn(
    context: RecordsContext, record: RequestRecord, *, first: bool
) -> tuple[Signal, ...]:
    """The cancellation that ends the turn of a dispatch core just ended, else nothing.

    A dispatch that ends at its limit (or a watch) with no result leaves its session executing
    with nothing to answer it. The turn is lost like one whose acceptance stayed unknown:
    Sessions cancels it and answers its invocation as lost, and the search's role decides
    whether to ask again.
    """
    if not first or not is_open(record) or record.phase == RequestPhase.PREPARED:
        return ()
    invocation = turn_of(context, record)
    if invocation is None:
        return ()
    return (
        InvocationCancellationRequested(
            invocation=invocation, authority=lost_turn_authority(invocation)
        ),
    )


def _decision_ended(state: RecordsState, record: RequestRecord) -> bool:
    """Whether another request of the record's decision already ended with a failure.

    A decision hears of its ending once. Its later requests (the submission a measurement
    still holds open after its observation timed out) end quietly: the owner has moved on.
    """
    decision = record.request.decision_id
    return decision is not None and any(
        row.failure is not None
        and row.request.decision_id == decision
        and row.request_id != record.request_id
        for row in state.records
    )


def _failure(record: RequestRecord, reason: FailureReason) -> Failed:
    """The typed result a record ends with: what ended it, and which watch or limit."""
    watch = next(
        (
            watch
            for watch in record.until.watches
            if (isinstance(watch, WhileOpen) and reason == FailureReason.WORKSPACE_CLOSED)
            or (isinstance(watch, WhileAlive) and reason == FailureReason.OWNER_GONE)
        ),
        None,
    )
    return Failed(
        reason=reason,
        decision=record.request.decision_id,
        watch=watch,
        limit_at=record.until.at if reason == FailureReason.TIMED_OUT else None,
    )


def _replace_record(state: RecordsState, record: RequestRecord) -> RecordsState:
    return state.model_copy(
        update={
            "records": tuple(
                record if row.request_id == record.request_id else row for row in state.records
            )
        }
    )


def _canonical(record: RequestRecord) -> bool:
    return not isinstance(record.request, InspectRequest | CancelOwnedResource | BlockRequest)


def is_open(record: RequestRecord) -> bool:
    """A canonical record that has not ended: core owes it a result by its ``until``."""
    return _canonical(record) and record.phase in (
        RequestPhase.PREPARED,
        RequestPhase.DISPATCHED,
        RequestPhase.RECONCILING,
    )


def command_open(record: RequestRecord) -> bool:
    """An inspection, cancel or block that has not ended: its limit ends it if nothing else."""
    return not _canonical(record) and record.phase in (
        RequestPhase.PREPARED,
        RequestPhase.DISPATCHED,
        RequestPhase.RECONCILING,
    )


def overdue_reconciliations(
    state: RecordsState, now_at: float
) -> tuple[ReconciliationDeadline | ReconciliationCommandLapsed, ...]:
    """One deadline event per open record, or lapse per open command, whose limit has passed."""
    return (
        *(
            ReconciliationDeadline(request_id=record.request_id, now_at=now_at)
            for record in state.records
            if is_open(record) and record.until.at <= now_at
        ),
        *(
            ReconciliationCommandLapsed(request_id=record.request_id, now_at=now_at)
            for record in state.records
            if command_open(record) and record.until.at <= now_at
        ),
    )


def next_reconciliation_at(state: RecordsState) -> float | None:
    """The earliest time a ``ClockAdvanced`` would end an open record at its limit."""
    return min(
        (
            max(record.until.at, _awaiting_report(state, record) or record.until.at)
            for record in state.records
            if is_open(record) or command_open(record)
        ),
        default=None,
    )


def advance(
    state: RecordsState, context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Own only recovery and children; canonical record facts remain ledger-owned."""
    match event:
        case RecoveryStarted():
            return _start(state, context, event)
        case RequestObserved():
            return _observe(state, context, event)
        case ReconciliationDeadline():
            return _deadline(state, context, event)
        case ReconciliationCommandLapsed():
            return _lapse_command(state, context, event)
        case RecoveryCheckWaitLapsed():
            return _recheck(state, context, event)
        case RecoveryReady():
            if event.epoch == state.recovery.epoch and state.recovery.phase != RecoveryPhase.READY:
                raise ContractError(
                    ("recovery",), "readiness requires resolved current-epoch checks"
                )
            return AreaChange(state=state)
        case _:
            raise ContractError(("event",), "event is not owned by record recovery")
