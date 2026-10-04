"""Authoritative outbox and class-driven operation observation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SerializeAsAny, ValidationInfo, model_validator

from vs_core._outcomes import OutcomeCodecError, OutcomeValue, bind_outcome

from .attempts import WorkspaceRequest
from .common import (
    CompletionStatus,
    ContractValidationError,
    Count,
    DecisionId,
    ExecuteRegisteredOperation,
    LifecycleClass,
    Observation,
    OperationId,
    OperationRef,
    OperationSchemaRef,
    RequestBase,
    RequestId,
    ResourceId,
    RevisionRef,
    SchemaRef,
    Scope,
    ScopeReopenNormalization,
    Seconds,
    Value,
)
from .evaluation import Continuation, EvaluationRequest, EvidenceRef
from .job_observations import JobProgress, MeasurementFailure
from .sessions import SessionRequest, TurnSpec
from .settlement import AdoptionRequest


class InspectRequest(RequestBase):
    """Inspect request lifecycle contract."""

    kind: Literal["inspect_request"] = "inspect_request"
    target: RequestId
    resource_id: ResourceId | None = None


class CancelOwnedResource(RequestBase):
    """Cancel owned resource lifecycle contract."""

    kind: Literal["cancel_owned_resource"] = "cancel_owned_resource"
    resource_id: ResourceId
    target: RequestId


class BlockIntent(RequestBase):
    """Block intent lifecycle contract."""

    kind: Literal["block_intent"] = "block_intent"
    target: RequestId
    diagnostic: str


type RegisteredOperationRequest = ExecuteRegisteredOperation
type ReconciliationRequest = Annotated[
    InspectRequest | CancelOwnedResource | BlockIntent, Field(discriminator="kind")
]
type Request = Annotated[
    WorkspaceRequest
    | SessionRequest
    | EvaluationRequest
    | AdoptionRequest
    | RegisteredOperationRequest
    | ReconciliationRequest,
    Field(discriminator="kind"),
]


class IntentPhase(StrEnum):
    """Intent phase lifecycle contract."""

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class Intent(OutcomeValue):
    """Intent lifecycle contract."""

    request_id: RequestId
    request: Request
    payload_digest: str
    lifecycle: LifecycleClass
    phase: IntentPhase
    sequence: Count | None = None
    observation: Observation | None = None
    suspension: Continuation | None = None
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    retry_count: Count = 0
    reconcile_deadline_at: Seconds

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> Intent:
        """Restore the owning subtype at the registered codec boundary."""
        return bind_outcome(self, info)


class ChildLease(Value):
    """Discovered descendant ownership, distinct from its ancestor's intent.

    Source requests and parent resources are unique, canonically ordered proofs.
    Conflicting scope or ancestry rejects; transfer to a typed owner is atomic.
    """

    resource_id: ResourceId
    scope: Scope
    source_requests: tuple[RequestId, ...] = Field(min_length=1)
    parent_resources: tuple[ResourceId, ...] = ()
    observation: Observation | None = None

    @model_validator(mode="after")
    def ownership_proofs(self) -> ChildLease:
        """Reject duplicate or noncanonical proof and resource correspondence."""
        for name, values in (
            ("source_requests", self.source_requests),
            ("parent_resources", self.parent_resources),
        ):
            roots = tuple(value.root for value in values)
            if roots != tuple(sorted(set(roots))):
                raise ContractValidationError(
                    name, "unique canonically ordered identities required"
                )
        if self.observation is not None and (
            self.observation.scope != self.scope or self.observation.resource_id != self.resource_id
        ):
            raise ContractValidationError(
                "observation", "child scope and resource_id must match its lease"
            )
        return self


class RecoveryPhase(StrEnum):
    """Recovery gates admission independently of the run's pause state."""

    REQUIRED = "required"
    RECOVERING = "recovering"
    READY = "ready"
    BLOCKED = "blocked"


class RecoveryCheck(Value):
    """Original target resolved by inspection, never command success alone."""

    target: RequestId
    inspection: RequestId | None = None
    resolution: Literal["pending", "safe-prepared", "reattached", "terminal", "blocked"] = "pending"


class RecoveryBarrier(Value):
    """Fence-epoch barrier persisted before ordinary admission or dispatch.

    READY requires every target safe to dispatch, positively reattached, or
    conclusively terminal. BLOCKED still permits bounded inspection and cleanup.
    Live jobs may remain owned when recovery finishes. Stale epochs cannot
    resolve checks from a newer startup.
    """

    epoch: Count = 0
    phase: RecoveryPhase = RecoveryPhase.REQUIRED
    checks: tuple[RecoveryCheck, ...] = ()

    @model_validator(mode="after")
    def ready_proofs(self) -> RecoveryBarrier:
        """No original target is checked twice or admitted without resolution."""
        targets = tuple(check.target for check in self.checks)
        if len(set(targets)) != len(targets):
            raise ContractValidationError("checks", "duplicate recovery target")
        if self.phase == RecoveryPhase.READY and any(
            check.resolution in ("pending", "blocked") for check in self.checks
        ):
            raise ContractValidationError(
                "checks", "READY requires safe-prepared, reattached or terminal proof"
            )
        return self


class IntentsState(Value):
    """Intents state lifecycle contract."""

    intents: tuple[Intent, ...] = ()
    children: tuple[ChildLease, ...] = ()
    recovery: RecoveryBarrier = RecoveryBarrier()


class OperationView(Value):
    """Operation view lifecycle contract."""

    operation_id: OperationId
    scope: Scope
    schema_ref: OperationSchemaRef
    phase: IntentPhase
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)


class RequestPrepared(Value):
    """Request prepared lifecycle contract."""

    kind: Literal["request_prepared"] = "request_prepared"
    request: Request
    lifecycle: LifecycleClass
    normalized_turn: TurnSpec | None = None
    normalized_scope_reopen: ScopeReopenNormalization | None = None


class DispatchAuthorized(Value):
    """Dispatch authorized lifecycle contract."""

    kind: Literal["dispatch_authorized"] = "dispatch_authorized"
    request_id: RequestId


class TargetObservation(OutcomeValue):
    """Lifecycle facts for the inspected target, separate from query completion.

    Its request ID names the original target. A child result names the exact
    child resource and cannot overwrite a parent observation or registered
    outcome. Root outcomes bind to the target descriptor. Query and target
    sequences are independent; command and target facts commit atomically.
    """

    observation: Observation
    target_resource: ResourceId | None = None
    revision: RevisionRef | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    progress: JobProgress | None = None
    measurement_failure: MeasurementFailure | None = None
    suspension: Continuation | None = None
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    operation_schema: OperationSchemaRef | None = None

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> TargetObservation:
        """Restore registered target subtypes at the owning codec boundary."""
        if self.target_resource is not None and (
            self.observation.resource_id != self.target_resource
        ):
            raise ContractValidationError(
                "target_resource", "must match the inspected child observation"
            )
        if self.target_resource is not None and any(
            value is not None
            for value in (
                self.operation_schema,
                self.outcome_schema,
                self.outcome_json,
                self.outcome,
            )
        ):
            raise ContractValidationError(
                "target_resource", "child lease cannot carry a root registered outcome"
            )
        if self.progress is not None and (
            self.progress.observation_sequence != self.observation.sequence
            or self.progress.observed_at != self.observation.observed_at
        ):
            raise ContractValidationError(
                "progress", "sequence and time must match its observation"
            )
        return bind_outcome(self, info)


class RequestObserved(OutcomeValue):
    """Request observed lifecycle contract."""

    kind: Literal["request_observed"] = "request_observed"
    target: TargetObservation | None = None
    progress: JobProgress | None = None
    measurement_failure: MeasurementFailure | None = None
    suspension: Continuation | None = None
    observation: Observation
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    evidence: tuple[EvidenceRef, ...] = ()
    revision: RevisionRef | None = None
    operation_schema: OperationSchemaRef | None = None

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> RequestObserved:
        """Restore the owning subtype at the registered codec boundary."""
        return bind_outcome(self, info)


class RecoveryStarted(Value):
    """Recovery started lifecycle contract."""

    kind: Literal["recovery_started"] = "recovery_started"
    epoch: Count
    now_at: Seconds


class RecoveryReady(Value):
    """Current-epoch recovery completion wakes scheduling without unpausing."""

    kind: Literal["recovery_ready"] = "recovery_ready"
    epoch: Count


class ReconciliationDeadline(Value):
    """Reconciliation deadline lifecycle contract."""

    kind: Literal["reconciliation_deadline"] = "reconciliation_deadline"
    request_id: RequestId
    now_at: Seconds


class DecisionDependencyResolved(Value):
    """Completion wakes intents to release or reject prepared dependents."""

    kind: Literal["decision_dependency_resolved"] = "decision_dependency_resolved"
    decision_id: DecisionId
    status: CompletionStatus


class OperationRetireRequested(Value):
    """Explicit retirement of a registered owned operation, resolved by intents."""

    kind: Literal["operation_retire_requested"] = "operation_retire_requested"
    operation: OperationRef
    scope: Scope


class OperationResult(OutcomeValue):
    """Operation result lifecycle contract."""

    kind: Literal["operation_result"] = "operation_result"
    operation_id: OperationId
    observation: Observation
    outcome_schema: SchemaRef
    operation_schema: OperationSchemaRef
    outcome_json: str | None = None
    # The wire omits this derived field. The after-validator requires and restores
    # the registered subtype before a callback can escape the codec.
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> OperationResult:
        """Give strategy callbacks the owner's validated model, never an opaque dict."""
        validated = bind_outcome(self, info)
        if validated.outcome is None:
            raise OutcomeCodecError("context")
        return validated


type IntentsEvent = Annotated[
    RequestPrepared
    | DispatchAuthorized
    | RequestObserved
    | RecoveryStarted
    | RecoveryReady
    | ReconciliationDeadline
    | OperationRetireRequested
    | DecisionDependencyResolved,
    Field(discriminator="kind"),
]
