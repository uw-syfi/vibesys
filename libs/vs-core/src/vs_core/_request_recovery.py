"""Pure recovery barriers and descendant ownership, separate from the ledger."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from . import _host_epochs, _wait_clock
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
    observation_for,
    released_owner,
    resolved_observation,
)
from ._recovery_owners import known_owner, operation_decision, typed_job_owner
from ._recovery_unanswered import Inspection, Settlement, settlement
from ._registry import ContractError
from ._request_ledger import resent
from ._waits import owned_job_subject, recovery_waits
from .types.attempts import (
    AttemptPhase,
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    RetainRevision,
    SnapshotAndRetain,
)
from .types.common import (
    AttemptId,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    OperationNormalizationKind,
    RequestId,
    RevisionAuthority,
    RunStatus,
    Scope,
)
from .types.evaluation import (
    CancelOwnedJob,
    RegisteredOwnedJob,
)
from .types.kernel import AreaChange
from .types.records import (
    BlockRequest,
    CancelOwnedResource,
    ChildLease,
    ChildObservationWatermark,
    InspectRequest,
    Outbound,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryCheckWaitLapsed,
    RecoveryPhase,
    RecoveryReady,
    RecoveryStarted,
    RequestObserved,
    RequestPhase,
    is_open,
    within,
)
from .types.scope_reopen import ScopedAdmissionReopenOutcome
from .types.sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    ResumeSessionTurn,
)
from .types.waits import WaitKind, WaitOn, WaitSubject

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .types.common import Observation, ResourceId
    from .types.kernel import RecordsContext, RunState
    from .types.records import (
        RecordsEvent,
        RecordsState,
        Request,
        RequestRecord,
    )

type Resolution = Literal[
    "pending", "safe-prepared", "reattached", "terminal", "unknown", "blocked"
]


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
    decision = operation_decision(record, context)
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
    if record.phase == RequestPhase.BLOCKED and record.lifecycle == LifecycleClass.QUERY:
        # Core ended this query at its limit, before the host that asked it died. It started
        # nothing and owns nothing, and nobody waits on it (its owner heard it blocked), so no
        # executor answer can matter: an inspection that finds it begun and unsealed (the host
        # died as the poll ran) must not hold the run in recovery, as it did a run whose old
        # polls of jobs core had long released came back unknown.
        # "terminal" settles only the recovery check, even for an Unknown observation;
        # it proves no external acceptance, termination or release and changes no ledger
        # facts. test_ambiguous_acceptance_never_replays_any_lifecycle_class exhausts this
        # exception and requires every other ambiguous class/phase to stay pending.
        return "terminal"
    return _observed_resolution(record, context)


def _observed_resolution(record: RequestRecord, context: RecordsContext) -> Resolution:
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
    # A cancel of a job reports ``cancelled``: the executor accepted the cancellation, and the
    # job it names is the submission's to release. Its observation never lists children, so
    # it can never prove a manifest: it was read as unresolved, and a recovery that inspected
    # it got the same facts back and waited for good.
    names_only = (
        not _owns_resource(record)
        and observation.accepted
        and (
            observation.status == ObservationStatus.SUCCEEDED
            or (
                isinstance(record.request, CancelOwnedJob)
                and observation.status == ObservationStatus.CANCELLED
            )
        )
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
        and known_owner(record, context, observation)
    ):
        return "reattached"
    return "pending"


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
                and typed_job_owner(source, context, observation, job)
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


def _waiting_on(
    state: RecordsState,
    context: RecordsContext,
    check: RecoveryCheck,
    sent: frozenset[RequestId],
    epoch: int,
) -> tuple[tuple[RequestId, ...], tuple[WaitSubject, ...]]:
    """The requests and jobs whose answer re-resolves *check*, the target's own included.

    Any committed answer about the target re-resolves the check (``_observe``): the target
    itself while it is open, and every inspection of it, the check's own, an earlier epoch's
    sent again, and the inspections of the children it left. A child its typed job ledger
    tracks settles when that job does, so the check names the job, but only when every child
    has one: a child nobody answers for keeps the check from ever resolving. *sent* holds the
    requests this transition sends.
    """
    record = state.record(check.target)
    children = [child for child in state.children if check.target in child.source_requests]
    expected = [check.target, check.inspection]
    if record is not None:
        expected += [
            _child_inspection(record, child.resource_id, epoch).request_id for child in children
        ]
    expected += [row.request_id for row in state.inspections_of(check.target)]
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
    return requests, subjects


def _check_wait(
    state: RecordsState,
    context: RecordsContext,
    check: RecoveryCheck,
    sent: frozenset[RequestId],
    epoch: int,
) -> WaitOn | None:
    """What a pending check waits on now: the requests and jobs whose answer re-resolves it.

    A check left with nothing keeps the wait it had, naming what it waited on, so the run
    halts with it named (`_settle_unanswered` settles one nothing can ever answer first).
    """
    if check.resolution != "pending":
        return None
    requests, subjects = _waiting_on(state, context, check, sent, epoch)
    if not requests and not subjects:
        return check.wait
    return WaitOn(requests=requests, subjects=subjects)


_NO_LIMIT = 1.7976931348623157e308
"""The limit of a request with none: a run with no deadline gives its requests the largest float
(``Seconds`` allows no infinity)."""


def _unbounded(state: RecordsState, context: RecordsContext, record: RequestRecord) -> bool:
    """Whether no limit will end *record*: it is not open, or its limit is the end of time."""
    if not is_open(record):
        return True
    bound = _wait_clock.record_bound(record, context.evaluation, context.run, state.hosts)
    return bound.at >= _NO_LIMIT


def _inspection_of(state: RecordsState, check: RecoveryCheck) -> Inspection:
    """What became of the inspection this epoch sent about the check's target."""
    if check.inspection is None:
        return Inspection.NONE
    row = state.record(check.inspection)
    answer = None if row is None else row.observation
    # An inspection the executor failed to run until its retries or its bound ran out says
    # nothing about the target; one it ran, whatever it found, answered.
    if (
        row is None
        or row.phase == RequestPhase.BLOCKED
        or answer is None
        or answer.status != ObservationStatus.SUCCEEDED
        or not answer.accepted
    ):
        return Inspection.UNREACHABLE
    return Inspection.ANSWERED


_VERDICT: Mapping[Settlement, Resolution] = MappingProxyType(
    {
        "resend": "safe-prepared",
        "unknown": "unknown",
        "terminal": "terminal",
        "blocked": "blocked",
    }
)


def _settle_unanswered(
    state: RecordsState,
    context: RecordsContext,
    barrier: RecoveryBarrier,
    sent: frozenset[RequestId],
) -> tuple[RecordsState, RecoveryBarrier]:
    """Settle every pending check that nothing is left to answer.

    The inspections about its target have all ended and no child job or session can move it:
    waiting more would hold recovery open in silence. The policy is `settlement`; a ``resend``
    puts the record back to PREPARED, which the check then reads as safe to dispatch. Only
    ``unknown`` (a result that is discarded) settles at once: any other verdict about an open
    record with a limit waits for that limit, which ends the record and blocks its check.
    """
    checks = list(barrier.checks)
    for index, check in enumerate(checks):
        record = state.record(check.target)
        if check.resolution != "pending" or record is None:
            continue
        requests, subjects = _waiting_on(state, context, check, sent, barrier.epoch)
        # The target itself answers nothing: its host is dead, and it is not dispatched yet.
        if subjects or any(request != check.target for request in requests):
            continue
        verdict = settlement(
            record,
            inspection=_inspection_of(state, check),
            owes_release=any(
                check.target in child.source_requests and not _child_proven(child, state)
                for child in state.children
            ),
            resendable=_prepared_episode(record, context),
        )
        if verdict != "unknown" and not _unbounded(state, context, record):
            continue  # its own limit ends it when that passes, as it ends any open record
        if verdict == "resend" and record.phase != RequestPhase.PREPARED:
            state = _replace_record(
                state,
                resent(record, context.run.now_at, context.run.limits.reconciliation_bound),
            )
        checks[index] = check.model_copy(update={"resolution": _VERDICT[verdict], "wait": None})
    return state, barrier.model_copy(update={"checks": tuple(checks)})


def _owner_sessions(
    record: RequestRecord | None, context: RecordsContext
) -> tuple[WaitSubject, ...]:
    """The waiting sessions an ensure's check waits on to learn whether they own its resource.

    An ensure resolves as reattached once its session is known to hold the resource it
    reported (``known_owner``). A session still settling its own state (unknown after a
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
    state, barrier = _settle_unanswered(state, context, barrier, sent_ids)
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


def _replace_record(state: RecordsState, record: RequestRecord) -> RecordsState:
    return state.model_copy(
        update={
            "records": tuple(
                record if row.request_id == record.request_id else row for row in state.records
            )
        }
    )


def advance(
    state: RecordsState, context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Own only recovery and children; canonical record facts remain ledger-owned."""
    match event:
        case RecoveryStarted():
            return _start(state, context, event)
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
