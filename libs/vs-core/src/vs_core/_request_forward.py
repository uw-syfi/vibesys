"""Translate a ledgered observation into the typed event its owning area consumes.

The record ledger commits an observation first. This module then names the one
owner event that carries the same fact onward, chosen only by the canonical
request class: nothing here reads or writes ledger state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import Proven, descriptor_matches
from ._registry import ContractError
from .types.attempts import (
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    ReleaseDependencyObserved,
    RetainRevision,
    RevisionOperationObserved,
    SnapshotAndRetain,
    WorkspaceObserved,
)
from .types.common import (
    AttemptRef,
    ExecuteRegisteredOperation,
    LifecycleClass,
    RevisionAuthority,
)
from .types.evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    InspectOwnedJob,
    JobObserved,
    MeasurementSubmissionObserved,
    ObserveOwnedJob,
    RegisteredJobObserved,
    SubmitMeasurement,
)
from .types.records import reply_owed
from .types.sessions import (
    CloseSession,
    DispatchTurn,
    EnsureSession,
    ResumeSessionTurn,
    RunInvocationCheckpointObserved,
    SessionObserved,
    SnapshotAndRetainRun,
    TurnObserved,
)
from .types.settlement import AdoptionObserved, AdoptRevision, VerifyAdoption

if TYPE_CHECKING:
    from .types.common import InvocationRef, Observation, OperationDescriptor, SchemaRef
    from .types.kernel import RecordsContext, Signal
    from .types.records import RequestObserved, RequestRecord, TargetObservation
    from .types.sessions import Invocation

type Facts = RequestObserved | TargetObservation


def declaration(
    context: RecordsContext, request: ExecuteRegisteredOperation
) -> OperationDescriptor:
    """The single registered declaration of an operation, named when absent or ambiguous."""
    rows = tuple(row for row in context.registry if row.kind == request.operation.schema_ref.kind)
    if len(rows) != 1:
        raise ContractError(("operation", "kind"), "unregistered or ambiguous operation")
    proof = descriptor_matches(
        context.registry,
        context.run.capabilities,
        request.operation,
        request.operation.schema_ref.lifecycle,
    )
    if not isinstance(proof, Proven):
        raise ContractError(("operation",), "requires an exact offered declaration")
    return proof.value


def records_own(descriptor: OperationDescriptor) -> bool:
    """Queries and plain idempotent writes are completed by the ledger itself."""
    return (
        descriptor.lifecycle in (LifecycleClass.QUERY, LifecycleClass.IDEMPOTENT_WRITE)
        and descriptor.revision_authority == RevisionAuthority.NONE
    )


def turn_of(context: RecordsContext, record: RequestRecord) -> InvocationRef | None:
    """The invocation a turn-dispatching record carries, or None for any other record."""
    request = record.request
    if isinstance(request, DispatchTurn | ResumeSessionTurn) or (
        isinstance(request, ExecuteRegisteredOperation)
        and record.lifecycle == LifecycleClass.SESSION_TURN
    ):
        return _invocation(context, record)
    return None


def _invocation(context: RecordsContext, record: RequestRecord) -> InvocationRef:
    request = record.request
    matches: tuple[Invocation, ...]
    if isinstance(request, ExecuteRegisteredOperation):
        matches = tuple(
            row
            for row in context.sessions.invocations
            if row.registered_operation == request.operation_id and row.scope == request.scope
        )
    elif isinstance(request, DispatchTurn | ResumeSessionTurn):
        matches = tuple(
            row
            for row in context.sessions.invocations
            if row.turn == request.turn and row.scope == request.scope
        )
    else:
        matches = ()
    if len(matches) != 1:
        raise ContractError(("request_id",), "turn observation needs one canonical invocation")
    return matches[0].invocation


def _registered(
    context: RecordsContext,
    record: RequestRecord,
    request: ExecuteRegisteredOperation,
    facts: Facts,
) -> tuple[Signal, ...]:
    observation = facts.observation
    descriptor = declaration(context, request)
    if descriptor.revision_authority != RevisionAuthority.NONE:
        return (
            RevisionOperationObserved(
                operation_id=request.operation_id, observation=observation, revision=facts.revision
            ),
        )
    match descriptor.lifecycle:
        case LifecycleClass.SESSION_TURN:
            return (
                TurnObserved(
                    invocation=_invocation(context, record),
                    observation=observation,
                    suspension=facts.suspension,
                    output_schema=_output_schema(facts),
                    output_json=facts.outcome_json,
                    reply_errors=facts.reply_errors,
                ),
            )
        case LifecycleClass.OWNED_JOB:
            return (
                RegisteredJobObserved(
                    progress=facts.progress,
                    operation_id=request.operation_id,
                    observation=observation,
                    evidence=facts.evidence,
                    evaluation_result=facts.evaluation_result,
                ),
            )
        case _:
            return ()


def _output_schema(facts: Facts) -> SchemaRef | None:
    if facts.outcome_schema is not None:
        return facts.outcome_schema
    return None if facts.operation_schema is None else facts.operation_schema.outcome_schema


def _releases(
    context: RecordsContext, record: RequestRecord, observation: Observation
) -> tuple[Signal, ...]:
    """Terminal acknowledgement of a request that a closing attempt waits on.

    A session close is not released here: Sessions emits that edge itself, after it
    has checked the lease proof, and a second emitter would deliver it twice.
    """
    if not observation.terminal:
        return ()
    return tuple(
        ReleaseDependencyObserved(
            attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
            dependency=edge,
            observation=observation,
        )
        for owner in context.attempts.attempts
        if owner.closure is not None
        for edge in owner.release_dependencies
        if edge.kind in ("request", "workspace") and edge.identity == record.request_id
    )


def owner_signals(
    context: RecordsContext, record: RequestRecord, facts: Facts
) -> tuple[Signal, ...]:
    """The owner events for one fresh observation of this record's canonical request."""
    return (*_owner_signal(context, record, facts), *_releases(context, record, facts.observation))


def _owner_signal(
    context: RecordsContext, record: RequestRecord, facts: Facts
) -> tuple[Signal, ...]:
    request = record.request
    observation = facts.observation
    signals: tuple[Signal, ...] = ()
    match request:
        case ExecuteRegisteredOperation():
            signals = _registered(context, record, request, facts)
        case (
            EnsureWorkspace()
            | SnapshotAndRetain()
            | RetainRevision()
            | DiscardWorkspace()
            | CloseAttemptScope()
        ):
            signals = (
                WorkspaceObserved(
                    attempt=request.attempt, observation=observation, revision=facts.revision
                ),
            )
        case EnsureSession() | CloseSession():
            session = (
                request.spec.session_id
                if isinstance(request, EnsureSession)
                else request.session_id
            )
            signals = (
                SessionObserved(
                    session_id=session, observation=observation, failure=facts.setup_failure
                ),
            )
        case DispatchTurn() | ResumeSessionTurn():
            if not reply_owed(observation, facts):
                signals = (
                    TurnObserved(
                        invocation=_invocation(context, record),
                        observation=observation,
                        suspension=facts.suspension,
                        output_schema=facts.outcome_schema,
                        output_json=facts.outcome_json,
                        reply_errors=facts.reply_errors,
                    ),
                )
        case SnapshotAndRetainRun():
            signals = (
                RunInvocationCheckpointObserved(
                    invocation=request.invocation,
                    checkpoint_request=observation.request_id,
                    observation=observation,
                    revision=facts.revision,
                ),
            )
        case SubmitMeasurement():
            signals = (
                MeasurementSubmissionObserved(
                    observation=observation, failure=facts.measurement_failure
                ),
            )
        case ObserveOwnedJob() | InspectOwnedJob() | CollectEvidence() | CancelOwnedJob():
            signals = (
                JobObserved(
                    progress=facts.progress,
                    resource_id=observation.resource_id or request.resource_id,
                    observation=observation,
                    evidence=facts.evidence,
                    evaluation_result=facts.evaluation_result,
                ),
            )
        case AdoptRevision() | VerifyAdoption():
            signals = (
                AdoptionObserved(
                    observation=observation,
                    revision=facts.revision,
                    publication=facts.publication,
                    checkout=facts.checkout,
                ),
            )
        case _:
            pass
    return signals
