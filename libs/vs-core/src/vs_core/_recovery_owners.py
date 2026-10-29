"""Which typed owner holds the resource a request reported: what recovery reattaches on.

A request that reported a live resource (a job, a session, a workspace) resolves as reattached once
the area that owns it (the evaluation ledger, sessions, attempts) is known to hold that resource.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import (
    Proven,
    current_admission,
    descriptor_matches,
    invocation_for,
    observation_for,
    operation_for,
    submission_budget_for,
)
from .types.attempts import (
    AttemptPhase,
    EnsureWorkspace,
)
from .types.common import (
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    OperationNormalizationKind,
)
from .types.evaluation import (
    OwnedJob,
    RegisteredOwnedJob,
    SubmitMeasurement,
)
from .types.records import (
    RequestPhase,
)
from .types.sessions import (
    DispatchTurn,
    EnsureSession,
    ResumeSessionTurn,
    SessionPhase,
)

if TYPE_CHECKING:
    from .types.common import Observation
    from .types.kernel import RecordsContext
    from .types.records import (
        RequestRecord,
    )
    from .types.sessions import Invocation, SessionView
    from .types.strategy import Operation


def operation_decision(record: RequestRecord, context: RecordsContext) -> Operation | None:
    """The decision that asked for a registered operation, or None when it is not proven."""
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
        decision = operation_decision(record, context)
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


def typed_job_owner(
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
    decision = operation_decision(record, context)
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


def known_owner(record: RequestRecord, context: RecordsContext, observation: Observation) -> bool:
    """Whether an area already owns the resource ``observation`` of ``record`` reports."""
    if any(
        typed_job_owner(record, context, observation, job)
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
