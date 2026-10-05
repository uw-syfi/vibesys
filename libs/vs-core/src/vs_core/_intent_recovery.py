"""Pure recovery barriers and descendant ownership, separate from the ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

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
    Scope,
)
from .types.evaluation import (
    CancelOwnedJob,
    OwnedJob,
    RegisteredOwnedJob,
    SubmitMeasurement,
)
from .types.intents import (
    BlockIntent,
    CancelOwnedResource,
    ChildLease,
    ChildObservationWatermark,
    InspectRequest,
    IntentBlocked,
    IntentPhase,
    ReconciliationDeadline,
    RecoveryBarrier,
    RecoveryCheck,
    RecoveryPhase,
    RecoveryReady,
    RecoveryStarted,
    RequestObserved,
)
from .types.kernel import AreaChange
from .types.scope_reopen import ScopedAdmissionReopenOutcome
from .types.sessions import CancelTurn, CloseSession, DispatchTurn, EnsureSession, ResumeSessionTurn

if TYPE_CHECKING:
    from .types.common import Observation, ResourceId
    from .types.intents import Intent, IntentsEvent, IntentsState, Request
    from .types.kernel import IntentsContext
    from .types.sessions import Invocation
    from .types.strategy import Operation

type Resolution = Literal["pending", "safe-prepared", "reattached", "terminal", "blocked"]


def _child_sources(child: ChildLease, state: IntentsState) -> Verdict[tuple[Observation, ...]]:
    """Complete, exact per-source facts are the only child ownership proof."""
    if not child.watermark_history_complete:
        return Missing(ProofReason.INCOMPLETE_HISTORY)
    marks = {mark.source_request: mark.observation for mark in child.observation_watermarks}
    if set(marks) != set(child.source_requests):
        return Mismatch(ProofField.REQUEST_ID)
    for source, observation in marks.items():
        sources = tuple(intent for intent in state.intents if intent.request_id == source)
        if len(sources) != 1 or sources[0].request.scope != child.scope:
            return Mismatch(ProofField.REQUEST_ID)
        intent = sources[0]
        if (
            not isinstance(observation_for(intent, observation), Proven)
            or observation.resource_id != child.resource_id
        ):
            return Mismatch(ProofField.ADMISSION_ID)
    return Proven(tuple(marks.values()))


def _child_query(state: IntentsState, event: RequestObserved) -> Verdict[Intent]:
    queries = tuple(row for row in state.intents if row.request_id == event.observation.request_id)
    if len(queries) > 1:
        return Mismatch(ProofField.REQUEST_ID)
    if not queries:
        return Missing(ProofReason.ABSENT_REQUEST)
    query = queries[0]
    source = observation_for(query, event.observation)
    return Proven(query) if isinstance(source, Proven) else source


def _child_inspection_fact(
    state: IntentsState, event: RequestObserved
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
        query.phase != IntentPhase.COMPLETED
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
    source = next(
        (row for row in state.intents if row.request_id == target.observation.request_id), None
    )
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


def _operation_decision(intent: Intent, context: IntentsContext) -> Operation | None:
    request = intent.request
    if not isinstance(request, ExecuteRegisteredOperation):
        return None
    proof = operation_for(context.run.receipts, request)
    return proof.value if isinstance(proof, Proven) else None


def _invocation_owner(
    intent: Intent, context: IntentsContext, invocation: Invocation, observation: Observation
) -> bool:
    request = intent.request
    registered = None
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        turn = request.turn
    elif isinstance(request, ExecuteRegisteredOperation):
        decision = _operation_decision(intent, context)
        if (
            decision is None
            or decision.registered_turn is None
            or request.operation.schema_ref.lifecycle != LifecycleClass.SESSION_TURN
            or intent.lifecycle != LifecycleClass.SESSION_TURN
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
                isinstance(observation_for(intent, previous), Proven)
                and previous.resource_id in (None, observation.resource_id)
            )
        )
    )


def _typed_job_owner(
    intent: Intent,
    context: IntentsContext,
    observation: Observation,
    job: OwnedJob | RegisteredOwnedJob,
) -> bool:
    """One canonical job correspondence for reattachment and lease transfer."""
    request = intent.request
    if (
        not isinstance(observation_for(intent, observation), Proven)
        or job.scope != observation.scope
        or job.resource_id not in (None, observation.resource_id)
        or (
            job.observation is not None
            and (
                not isinstance(observation_for(intent, job.observation), Proven)
                or job.observation.resource_id not in (None, observation.resource_id)
            )
        )
    ):
        return False
    if isinstance(job, OwnedJob):
        return (
            isinstance(request, SubmitMeasurement)
            and job.submission_id == intent.request_id
            and job.plan == request.plan
        )
    if not isinstance(request, ExecuteRegisteredOperation):
        return False
    decision = _operation_decision(intent, context)
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
        and job.request_id == intent.request_id
        and job.operation_id == request.operation_id
        and job.expected_measurement == decision.registered_measurement
    )


def _submission_owner(intent: Intent, context: IntentsContext) -> bool:
    if not isinstance(intent.request, SubmitMeasurement):
        return False
    return isinstance(
        submission_budget_for(
            intent.request, context.evaluation.submission_budgets, context.run.receipts
        ),
        Proven,
    )


def _workspace_owner(intent: Intent, context: IntentsContext, observation: Observation) -> bool:
    request = intent.request
    if not isinstance(request, EnsureWorkspace):
        return False
    # The attempt keeps the ensure request pending only while it acquires. Once it
    # consumed the accepted answer (it left ACQUIRING) the workspace is the attempt's own,
    # and a restart must still recognise the resource as owned: otherwise the recovery
    # check of a completed ensure never resolves.
    consumed = (
        intent.phase == IntentPhase.COMPLETED
        and observation.accepted
        and observation.status == ObservationStatus.SUCCEEDED
    )
    return any(
        owner.attempt_id == request.attempt.attempt_id
        and owner.generation == request.attempt.generation
        and isinstance(current_admission(owner, observation.scope, request.admission_id), Proven)
        and owner.workspace == request.plan
        and (
            intent.request_id in owner.pending_intents
            or (consumed and owner.phase != AttemptPhase.ACQUIRING)
        )
        for owner in context.attempts.attempts
    )


def _known_owner(intent: Intent, context: IntentsContext, observation: Observation) -> bool:
    if any(
        _typed_job_owner(intent, context, observation, job)
        for job in (*context.evaluation.jobs, *context.evaluation.registered_jobs)
    ):
        return True
    if _submission_owner(intent, context) or _workspace_owner(intent, context, observation):
        return True
    if isinstance(intent.request, EnsureSession) and any(
        session.scope == observation.scope
        and session.resource_id in (None, observation.resource_id)
        and session.spec == intent.request.spec
        and session.generation == observation.scope.generation
        and (
            intent.request.required_resource is None
            or observation.resource_id == intent.request.required_resource
        )
        and (
            (session.accepted and session.resource_id is not None)
            or intent.request_id in session.pending_intents
        )
        for session in context.sessions.sessions
    ):
        return True
    return any(
        _invocation_owner(intent, context, invocation, observation)
        for invocation in context.sessions.invocations
    )


def _prepared_episode(intent: Intent, context: IntentsContext) -> bool:
    request = intent.request
    if intent.lifecycle == LifecycleClass.QUERY or isinstance(
        request, CancelOwnedResource | CancelTurn | CancelOwnedJob | BlockIntent
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
            intent.lifecycle,
            OperationNormalizationKind.NONE,
        )
        cleanup = isinstance(descriptor, Proven) and descriptor.value.revision_authority in (
            RevisionAuthority.SNAPSHOT,
            RevisionAuthority.RETAIN,
            RevisionAuthority.DISCARD,
        )
    if not cleanup:
        return False
    identities = {intent.request_id}
    if isinstance(request, ExecuteRegisteredOperation):
        identities.add(request.operation_id)
    if isinstance(request, CloseSession):
        identities.add(request.session_id)
    closure = current_closure(owner, owner.closure)
    return isinstance(closure, Proven) and (
        closure.value.authority == intent.request_id
        or any(edge.identity in identities for edge in owner.release_dependencies)
    )


def _reopen_resolved(intent: Intent, context: IntentsContext) -> bool:
    request = intent.request
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
        intent.lifecycle,
        declarations[0].normalization,
    )
    if not isinstance(descriptor, Proven):
        return False
    decision = _operation_decision(intent, context)
    if decision is None:
        return False
    if descriptor.value.normalization != OperationNormalizationKind.SCOPE_REOPEN:
        return True
    outcome = intent.outcome
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


def _session_resources(intent: Intent, context: IntentsContext) -> set[ResourceId]:
    request = intent.request
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
        and intent.request_id in session.pending_intents
        and session.resource_id is not None
    }


def _resource_identified(intent: Intent, context: IntentsContext, observation: Observation) -> bool:
    request = intent.request
    resource_bearing = intent.lifecycle in (
        LifecycleClass.OWNED_JOB,
        LifecycleClass.SESSION_TURN,
    ) or isinstance(request, EnsureSession | EnsureWorkspace | CloseSession)
    if not resource_bearing:
        return True
    if observation.resource_id is None:
        return not observation.accepted and observation.status in (
            ObservationStatus.REJECTED,
            ObservationStatus.FAILED,
            ObservationStatus.CANCELLED,
        )
    expected = _session_resources(intent, context)
    if isinstance(request, EnsureSession) and request.required_resource is not None:
        expected.add(request.required_resource)
    expected.update(
        invocation.observation.resource_id
        for invocation in context.sessions.invocations
        if invocation.observation is not None
        and invocation.observation.request_id == intent.request_id
        and invocation.observation.scope == request.scope
        and invocation.observation.admission_id == request.admission_id
        and invocation.observation.resource_id is not None
    )
    return not expected or expected == {observation.resource_id}


def _resolution(intent: Intent, context: IntentsContext) -> Resolution:
    observation = intent.observation
    if intent.phase == IntentPhase.PREPARED and observation is None:
        return "safe-prepared" if _prepared_episode(intent, context) else "pending"
    if (
        not isinstance(observation_for(intent, observation), Proven)
        or observation is None
        or observation.status == ObservationStatus.UNKNOWN
        or not _reopen_resolved(intent, context)
    ):
        return "pending"
    if not _resource_identified(intent, context, observation):
        return "pending"
    terminal = observation.terminal and observation.status != ObservationStatus.PENDING
    resource_free = (
        intent.lifecycle not in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
        and observation.resource_id is None
        and not observation.children
        and observation.children_complete
    )
    if terminal and (
        intent.lifecycle == LifecycleClass.QUERY
        or resource_free
        or isinstance(released_owner(intent, (intent,)), Proven)
    ):
        return "terminal"
    if (
        observation.accepted
        and observation.resource_id is not None
        and observation.children_complete
        and _known_owner(intent, context, observation)
    ):
        return "reattached"
    return "pending"


def _inspection(intent: Intent, epoch: int) -> InspectRequest:
    return InspectRequest(
        request_id=RequestId(root=f"recovery:{epoch}:{intent.request_id.root}"),
        scope=intent.request.scope,
        admission_id=intent.request.admission_id,
        target=intent.request_id,
        deadline_at=intent.reconcile_deadline_at,
    )


def _child_inspection(intent: Intent, resource: ResourceId, epoch: int) -> InspectRequest:
    return _inspection(intent, epoch).model_copy(
        update={
            "request_id": RequestId(
                root=f"recovery:child:{epoch}:{len(intent.request_id.root)}:{intent.request_id.root}:{resource.root}"
            ),
            "resource_id": resource,
        }
    )


def _child_released(child: ChildLease, state: IntentsState) -> bool:
    return isinstance(released_owner(child, state.intents), Proven)


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


def _child_proven(child: ChildLease, state: IntentsState) -> bool:
    proof = _child_sources(child, state)
    return isinstance(proof, Proven) and all(
        _child_ready(child.model_copy(update={"observation": row})) for row in proof.value
    )


def _validate_children(state: IntentsState) -> None:
    records = {intent.request_id: intent for intent in state.intents}
    for index, child in enumerate(state.children):
        admissions = set()
        for source in child.source_requests:
            intent = records.get(source)
            if intent is None or intent.request.scope != child.scope:
                raise ContractError(
                    ("children", index, "source_requests", source.root),
                    "child requires a canonical source in its exact scope",
                )
            admissions.add(intent.request.admission_id)
        if len(admissions) > 1:
            raise ContractError(
                ("children", index, "source_requests"),
                "conflicting child ownership admission episodes require distinct resource leases",
            )


def _transfer_children(state: IntentsState, context: IntentsContext) -> IntentsState:
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
            intent.request_id in child.source_requests
            and intent.phase == IntentPhase.PREPARED
            and intent.observation is None
            for intent in state.intents
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
            source = next(
                (intent for intent in state.intents if intent.request_id == request_id), None
            )
            ancestor = next(
                intent for intent in state.intents if intent.request_id == child.source_requests[0]
            )
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


def _aggregate_resolution(
    intent: Intent, context: IntentsContext, state: IntentsState
) -> Resolution:
    resolution = _resolution(intent, context)
    if resolution == "safe-prepared" and any(
        intent.request_id in child.source_requests for child in state.children
    ):
        return "pending"
    if any(
        intent.request_id in child.source_requests and not _child_proven(child, state)
        for child in state.children
    ):
        return "pending"
    return resolution


def _finish(state: IntentsState, barrier: RecoveryBarrier) -> AreaChange[IntentsState]:
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


def _start(
    state: IntentsState, context: IntentsContext, event: RecoveryStarted
) -> AreaChange[IntentsState]:
    barrier = state.recovery
    _validate_children(state)
    if event.epoch < barrier.epoch or (
        event.epoch == barrier.epoch and barrier.phase != RecoveryPhase.REQUIRED
    ):
        return AreaChange(state=state)
    updated = state
    for intent in state.intents:
        proof = observation_for(intent, intent.observation)
        if isinstance(proof, Proven):
            updated = _observe_children(updated, RequestObserved(observation=proof.value))
    checks: list[RecoveryCheck] = []
    requests: list[Request] = []
    targets = {intent.request_id for intent in state.intents}
    _validate_children(updated)
    updated = _transfer_children(updated, context)
    for intent in state.intents:
        if (
            isinstance(intent.request, InspectRequest | CancelOwnedResource | BlockIntent)
            and intent.request.target in targets
            and not any(
                intent.request_id in child.source_requests and not _child_proven(child, updated)
                for child in updated.children
            )
        ):
            continue
        resolution = _aggregate_resolution(intent, context, updated)
        inspection = (
            _inspection(intent, event.epoch)
            if resolution == "pending"
            and _resolution(intent, context) not in ("terminal", "reattached")
            else None
        )
        checks.append(
            RecoveryCheck(
                target=intent.request_id,
                inspection=inspection.request_id if inspection is not None else None,
                resolution=resolution,
            )
        )
        if inspection is not None:
            requests.append(
                inspection.model_copy(
                    update={
                        "deadline_at": max(context.run.now_at, event.now_at)
                        + context.run.limits.reconciliation_bound
                    }
                )
            )
        requests.extend(
            _child_inspection(intent, child.resource_id, event.epoch).model_copy(
                update={
                    "deadline_at": max(context.run.now_at, event.now_at)
                    + context.run.limits.reconciliation_bound
                }
            )
            for child in updated.children
            if intent.request_id in child.source_requests and not _child_proven(child, updated)
        )
    change = _finish(
        updated,
        RecoveryBarrier(epoch=event.epoch, phase=RecoveryPhase.RECOVERING, checks=tuple(checks)),
    )
    return change.model_copy(update={"requests": tuple(requests)})


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


def _source_observation(state: IntentsState, event: RequestObserved) -> Observation:
    observation = event.target.observation if event.target is not None else event.observation
    sources = tuple(
        intent for intent in state.intents if intent.request_id == observation.request_id
    )
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


def _observe_fact(state: IntentsState, event: RequestObserved) -> IntentsState:
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
        intent = next(
            (row for row in state.intents if row.request_id == observation.request_id), None
        )
        if intent is None or observation.scope != intent.request.scope:
            raise ContractError(("observation",), "unknown canonical ownership source")
        canonical = intent.observation
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


def _observe_children(state: IntentsState, event: RequestObserved) -> IntentsState:
    if event.target is not None:
        state = _observe_fact(state, event.model_copy(update={"target": None}))
    updated = _observe_fact(state, event)
    _validate_children(updated)
    return updated


def _child_release_signals(
    before: IntentsState, after: IntentsState, context: IntentsContext
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


def _inspection_exists(state: IntentsState, inspection: InspectRequest) -> bool:
    previous = next((row for row in state.intents if row.request_id == inspection.request_id), None)
    if previous is None:
        return False
    if not isinstance(previous.request, InspectRequest) or any(
        getattr(previous.request, field) != getattr(inspection, field)
        for field in ("scope", "admission_id", "target", "resource_id")
    ):
        raise ContractError(("request_id",), "inspection successor identity conflict")
    return True


def _pending_probes(
    state: IntentsState, context: IntentsContext, barrier: RecoveryBarrier
) -> tuple[tuple[RecoveryCheck, ...], tuple[Request, ...]]:
    checks = list(barrier.checks)
    requests = []
    for intent in state.intents:
        children = tuple(
            child
            for child in state.children
            if intent.request_id in child.source_requests and not _child_proven(child, state)
        )
        if not children:
            continue
        check = next((check for check in checks if check.target == intent.request_id), None)
        if check is None:
            inspection = (
                _inspection(intent, barrier.epoch)
                if _resolution(intent, context) not in ("terminal", "reattached")
                else None
            )
            checks.append(
                RecoveryCheck(
                    target=intent.request_id,
                    inspection=inspection.request_id if inspection is not None else None,
                )
            )
            if inspection is not None and not _inspection_exists(state, inspection):
                requests.append(
                    inspection.model_copy(
                        update={
                            "deadline_at": context.run.now_at
                            + context.run.limits.reconciliation_bound
                        }
                    )
                )
        elif check.resolution not in ("pending", "blocked"):
            inspection = (
                _inspection(intent, barrier.epoch)
                if _resolution(intent, context) not in ("terminal", "reattached")
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
                requests.append(
                    inspection.model_copy(
                        update={
                            "deadline_at": context.run.now_at
                            + context.run.limits.reconciliation_bound
                        }
                    )
                )
        for child in children:
            inspection = _child_inspection(intent, child.resource_id, barrier.epoch)
            if not _inspection_exists(state, inspection):
                requests.append(
                    inspection.model_copy(
                        update={
                            "deadline_at": context.run.now_at
                            + context.run.limits.reconciliation_bound
                        }
                    )
                )
    return tuple(checks), tuple(requests)


def _observe(
    state: IntentsState, context: IntentsContext, event: RequestObserved
) -> AreaChange[IntentsState]:
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
        intent = next((row for row in state.intents if row.request_id == check.target), None)
        child_resource = event.target.target_resource if event.target is not None else None
        expected_child = (
            _child_inspection(intent, child_resource, barrier.epoch).request_id
            if intent is not None and child_resource is not None
            else None
        )
        matches = (
            check.target == target
            and event.target is not None
            and (
                child_resource is not None
                or (intent is not None and intent.observation == event.target.observation)
            )
            and (
                check.inspection == event.observation.request_id
                if child_resource is None
                else expected_child == event.observation.request_id
            )
        )
        resolution = (
            _aggregate_resolution(intent, context, updated)
            if matches and intent is not None
            else check.resolution
        )
        checks.append(check.model_copy(update={"resolution": resolution}))
    change = _finish(updated, barrier.model_copy(update={"checks": tuple(checks)}))
    return change.model_copy(
        update={"signals": (*release_signals, *change.signals), "requests": requests}
    )


def _deadline_barrier(
    state: IntentsState, context: IntentsContext, intent: Intent, now_at: float
) -> tuple[RecoveryBarrier, InspectRequest | None]:
    if _aggregate_resolution(intent, context, state) == "reattached":
        return state.recovery, None
    inspection = _inspection(intent, state.recovery.epoch).model_copy(
        update={
            "request_id": RequestId(
                root=f"recovery:deadline:{state.recovery.epoch}:{intent.request_id.root}"
            ),
            "deadline_at": now_at + context.run.limits.reconciliation_bound,
        }
    )
    checks = list(state.recovery.checks)
    check = next((check for check in checks if check.target == intent.request_id), None)
    replacement = RecoveryCheck(
        target=intent.request_id, inspection=inspection.request_id, resolution="blocked"
    )
    if check is None:
        checks.append(replacement)
    else:
        checks[checks.index(check)] = replacement
    existing = next((row for row in state.intents if row.request_id == inspection.request_id), None)
    if existing is not None and (
        not isinstance(existing.request, InspectRequest)
        or existing.request.target != intent.request_id
        or existing.request.resource_id is not None
        or existing.request.scope != intent.request.scope
        or existing.request.admission_id != intent.request.admission_id
    ):
        raise ContractError(("request_id",), "inspection successor identity conflict")
    barrier = state.recovery.model_copy(
        update={"phase": RecoveryPhase.BLOCKED, "checks": tuple(checks)}
    )
    return barrier, inspection if existing is None else None


def _pending_cancellations(
    state: IntentsState, cancellations: tuple[CancelOwnedResource, ...]
) -> tuple[CancelOwnedResource, ...]:
    requests = []
    for cancellation in cancellations:
        previous = next(
            (row for row in state.intents if row.request_id == cancellation.request_id), None
        )
        if previous is None:
            requests.append(cancellation)
        elif not isinstance(previous.request, CancelOwnedResource) or any(
            getattr(previous.request, field) != getattr(cancellation, field)
            for field in ("scope", "admission_id", "target", "resource_id")
        ):
            raise ContractError(("request_id",), "cancellation successor identity conflict")
    return tuple(requests)


def _deadline_target(state: IntentsState, request_id: RequestId) -> Intent:
    records = {intent.request_id: intent for intent in state.intents}
    seen = set()
    while True:
        if request_id in seen:
            raise ContractError(("request", "target"), "cyclic reconciliation command ancestry")
        seen.add(request_id)
        intent = records.get(request_id)
        if intent is None:
            raise ContractError(("request_id", request_id.root), "unknown reconciliation target")
        request = intent.request
        if not isinstance(request, InspectRequest | CancelOwnedResource | BlockIntent):
            return intent
        target = records.get(request.target)
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


def _deadline(
    state: IntentsState, context: IntentsContext, event: ReconciliationDeadline
) -> AreaChange[IntentsState]:
    intent = _deadline_target(state, event.request_id)
    now_at = max(context.run.now_at, event.now_at)
    _validate_children(state)
    unresolved_children = any(
        intent.request_id in child.source_requests and not _child_released(child, state)
        for child in state.children
    )
    resolution = _resolution(intent, context)
    if now_at < intent.reconcile_deadline_at or (
        resolution in ("terminal", "reattached") and not unresolved_children
    ):
        return AreaChange(state=state)
    block_id = RequestId(root=f"recovery:block:{state.recovery.epoch}:{intent.request_id.root}")
    previous_block = next((row for row in state.intents if row.request_id == block_id), None)
    if previous_block is not None:
        request = previous_block.request
        if (
            not isinstance(request, BlockIntent)
            or request.target != intent.request_id
            or request.scope != intent.request.scope
            or request.admission_id != intent.request.admission_id
        ):
            raise ContractError(("request_id",), "reconciliation successor identity conflict")
    block = BlockIntent(
        request_id=block_id,
        scope=intent.request.scope,
        admission_id=intent.request.admission_id,
        deadline_at=max(context.run.now_at, event.now_at) + context.run.limits.reconciliation_bound,
        target=intent.request_id,
        diagnostic="reconciliation deadline reached without conclusive ownership proof",
    )
    requests: list[Request] = []
    events: tuple[IntentBlocked, ...] = ()
    if previous_block is None:
        requests.append(block)
        events = (
            IntentBlocked(
                request_id=block_id,
                target=block.target,
                scope=block.scope,
                diagnostic=block.diagnostic,
            ),
        )
    observation = intent.observation
    resources = []
    if (
        resolution != "reattached"
        and observation is not None
        and observation.accepted
        and observation.resource_id is not None
        and isinstance(observation_for(intent, observation), Proven)
        and _resource_identified(intent, context, observation)
        and not isinstance(resolved_observation(observation), Proven)
    ):
        resources.append(observation.resource_id)
    resources.extend(
        child.resource_id
        for child in state.children
        if child.scope == intent.request.scope
        and intent.request_id in child.source_requests
        and not _child_released(child, state)
    )
    cancellations = (
        CancelOwnedResource(
            request_id=RequestId(
                root=f"recovery:cancel:{state.recovery.epoch}:{len(intent.request_id.root)}:{intent.request_id.root}:{resource.root}"
            ),
            scope=intent.request.scope,
            admission_id=intent.request.admission_id,
            deadline_at=now_at + context.run.limits.cancellation_bound,
            resource_id=resource,
            target=intent.request_id,
        )
        for resource in sorted(set(resources), key=lambda value: value.root)
    )
    requests.extend(_pending_cancellations(state, tuple(cancellations)))
    barrier, inspection = _deadline_barrier(state, context, intent, now_at)
    if inspection is not None:
        requests.append(inspection)

    return AreaChange(
        state=state.model_copy(update={"recovery": barrier}),
        requests=tuple(requests),
        events=events,
    )


def advance(
    state: IntentsState, context: IntentsContext, event: IntentsEvent
) -> AreaChange[IntentsState]:
    """Own only recovery and children; canonical intent facts remain ledger-owned."""
    match event:
        case RecoveryStarted():
            return _start(state, context, event)
        case RequestObserved():
            return _observe(state, context, event)
        case ReconciliationDeadline():
            return _deadline(state, context, event)
        case RecoveryReady():
            if event.epoch != state.recovery.epoch:
                return AreaChange(state=state)
            if state.recovery.phase != RecoveryPhase.READY:
                raise ContractError(
                    ("recovery",), "readiness requires resolved current-epoch checks"
                )
            return AreaChange(state=state)
        case _:
            raise ContractError(("event",), "event is not owned by intent recovery")
