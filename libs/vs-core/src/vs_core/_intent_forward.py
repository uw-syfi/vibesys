"""Translate a ledgered observation into the typed event its owning area consumes.

The intent ledger commits an observation first. This module then names the one
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
    RestoreRevision,
    RetainRevision,
    RevisionOperationObserved,
    ScopeAdmissionReopened,
    SnapshotAndRetain,
    WorkspaceObserved,
)
from .types.common import (
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    OperationNormalizationKind,
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
from .types.scope_reopen import ScopedAdmissionReopenOutcome
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
    from .types.intents import Intent, RequestObserved, TargetObservation
    from .types.kernel import IntentsContext, Signal
    from .types.sessions import Invocation

type Facts = RequestObserved | TargetObservation


def declaration(
    context: IntentsContext, request: ExecuteRegisteredOperation
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
        rows[0].normalization,
    )
    if not isinstance(proof, Proven):
        raise ContractError(("operation",), "requires an exact offered declaration")
    return proof.value


def intents_own(descriptor: OperationDescriptor) -> bool:
    """Queries and plain idempotent writes are completed by the ledger itself."""
    return (
        descriptor.lifecycle in (LifecycleClass.QUERY, LifecycleClass.IDEMPOTENT_WRITE)
        and descriptor.revision_authority == RevisionAuthority.NONE
        and descriptor.normalization != OperationNormalizationKind.SCOPE_REOPEN
    )


def _invocation(context: IntentsContext, intent: Intent) -> InvocationRef:
    request = intent.request
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
    context: IntentsContext, intent: Intent, request: ExecuteRegisteredOperation, facts: Facts
) -> tuple[Signal, ...]:
    observation = facts.observation
    descriptor = declaration(context, request)
    if descriptor.normalization == OperationNormalizationKind.SCOPE_REOPEN:
        return _reopened(context, request, observation, facts)
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
                    invocation=_invocation(context, intent),
                    observation=observation,
                    suspension=facts.suspension,
                    output_schema=_output_schema(facts),
                    output_json=facts.outcome_json,
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


def _reopened(
    context: IntentsContext,
    request: ExecuteRegisteredOperation,
    observation: Observation,
    facts: Facts,
) -> tuple[Signal, ...]:
    receipt = next(
        (row for row in context.run.receipts if row.decision_id == request.decision_id), None
    )
    decision = None if receipt is None else receipt.decision
    normalization = getattr(decision, "normalized_scope_reopen", None)
    if normalization is None:
        raise ContractError(("operation",), "scope reopen requires its canonical normalization")
    outcome = facts.outcome
    admission = (
        outcome.admission if isinstance(outcome, ScopedAdmissionReopenOutcome) else "unknown"
    )
    if observation.status != ObservationStatus.SUCCEEDED or not observation.terminal:
        admission = "unknown" if not observation.terminal else "closed"
    return (
        ScopeAdmissionReopened(
            attempt=normalization.attempt,
            continuation_id=normalization.continuation_id,
            park_authority=normalization.park_authority,
            operation_id=request.operation_id,
            observation=observation,
            admission=admission,
        ),
    )


def owner_signals(context: IntentsContext, intent: Intent, facts: Facts) -> tuple[Signal, ...]:
    """The owner event for one fresh observation of this intent's canonical request."""
    request = intent.request
    observation = facts.observation
    signals: tuple[Signal, ...] = ()
    match request:
        case ExecuteRegisteredOperation():
            signals = _registered(context, intent, request, facts)
        case (
            EnsureWorkspace()
            | RestoreRevision()
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
            signals = (
                TurnObserved(
                    invocation=_invocation(context, intent),
                    observation=observation,
                    suspension=facts.suspension,
                    output_schema=facts.outcome_schema,
                    output_json=facts.outcome_json,
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
            signals = (AdoptionObserved(observation=observation, revision=facts.revision),)
        case _:
            pass
    return signals
