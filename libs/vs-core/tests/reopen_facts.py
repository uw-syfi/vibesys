"""Shared fixture: a parked owner whose queued reopening retirement can release."""

import vs_core.api as core

from .proof_digest import value_digest


def released_job(
    owner: core.AttemptView,
) -> tuple[core.RegisteredOwnedJob, core.Intent]:
    """A continuation job whose release is proved against its source request."""
    assert owner.closure is not None
    attempt = core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
    scope = core.Scope(owner=owner.attempt_id, generation=owner.generation)
    job_id = core.ResourceId(root="job")
    source_id = core.RequestId(root="job-source")
    source = core.CloseAttemptScope(
        request_id=source_id,
        scope=scope,
        attempt=attempt,
        admission_id=owner.closure.admission_id,
        deadline_at=100.0,
    )
    proof = core.Observation(
        event_id=core.EventId(root="job-released"),
        request_id=source_id,
        scope=scope,
        sequence=1,
        observed_at=2.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        resource_id=job_id,
        admission_id=owner.closure.admission_id,
    )
    job = core.RegisteredOwnedJob(
        operation_id=core.OperationId(root="job-operation"),
        request_id=source_id,
        scope=scope,
        resource_pool=core.PoolId(root="pool"),
        resource_id=job_id,
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        observation=proof,
    )
    intent = core.Intent(
        request_id=source_id,
        request=source,
        payload_digest=value_digest(source),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.COMPLETED,
        observation=proof,
        reconcile_deadline_at=100.0,
    )
    return job, intent


def with_reopening_continuation(
    state: core.CoreState,
    owner: core.AttemptView,
    *,
    continuation_id: core.ContinuationId,
    reopen_authority: core.RequestId,
) -> core.CoreState:
    """Add the REOPENING continuation Attempts needs to retire a queued reopen.

    Retirement owns a queued reopening only for a continuation whose suspended
    invocation belongs to the owner's scope. Its single job is already released,
    so the parked scope has no outstanding release dependency.
    """
    assert owner.closure is not None
    scope = core.Scope(owner=owner.attempt_id, generation=owner.generation)
    session = core.SessionId(root="session")
    job_id = core.ResourceId(root="job")
    continuation = core.Continuation(
        continuation_id=continuation_id,
        invocation=core.InvocationRef(
            session_id=session,
            invocation_id=core.InvocationId(root="suspended"),
            generation=owner.generation,
        ),
        next_invocation=core.InvocationRef(
            session_id=session,
            invocation_id=core.InvocationId(root="resumed"),
            generation=owner.generation,
        ),
        jobs=(job_id,),
        deadline_at=100.0,
        phase=core.ContinuationPhase.REOPENING,
        park_authority=owner.closure.authority,
        reopen_authority=reopen_authority,
    )
    suspended = core.Invocation(
        invocation=continuation.invocation,
        scope=scope,
        turn=core.TurnSpec(
            session=core.SessionSpec(
                session_id=session,
                role_id=core.RoleId(root="worker"),
                policy="fresh",
                lifetime="owner",
                access=core.Access.WRITE_CANDIDATE,
            ),
            invocation_id=continuation.invocation.invocation_id,
            workspace=scope,
            prompts=(),
            output_schema=core.SchemaRef(name="output", version=1),
            deadline_at=100.0,
            charge_class="paid",
        ),
        phase=core.SessionPhase.SUSPENDED,
    )
    job, intent = released_job(owner)
    return state.model_copy(
        update={
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
            "sessions": state.sessions.model_copy(update={"invocations": (suspended,)}),
            "evaluation": state.evaluation.model_copy(
                update={"continuations": (continuation,), "registered_jobs": (job,)}
            ),
        }
    )
