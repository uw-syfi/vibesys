"""Authoritative outbox and class-driven operation observation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SerializeAsAny, ValidationInfo, model_validator

from vs_core._outcomes import bind_outcome

from .attempts import WorkspaceRequest
from .common import (
    Count,
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
    Seconds,
    Value,
)
from .evaluation import EvaluationRequest, EvidenceRef
from .sessions import SessionRequest, TurnSpec
from .settlement import AdoptionRequest


class InspectRequest(RequestBase):
    """Inspect request lifecycle contract."""

    kind: Literal["inspect_request"] = "inspect_request"
    target: RequestId


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


class Intent(Value):
    """Intent lifecycle contract."""

    request_id: RequestId
    request: Request
    payload_digest: str
    lifecycle: LifecycleClass
    phase: IntentPhase
    sequence: Count | None = None
    observation: Observation | None = None
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    retry_count: Count = 0
    reconcile_deadline_at: Seconds

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> Intent:
        """Restore the owning subtype at the registered codec boundary."""
        return bind_outcome(self, info)


class IntentsState(Value):
    """Intents state lifecycle contract."""

    intents: tuple[Intent, ...] = ()


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


class DispatchAuthorized(Value):
    """Dispatch authorized lifecycle contract."""

    kind: Literal["dispatch_authorized"] = "dispatch_authorized"
    request_id: RequestId


class RequestObserved(Value):
    """Request observed lifecycle contract."""

    kind: Literal["request_observed"] = "request_observed"
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
    now_at: Seconds


class ReconciliationDeadline(Value):
    """Reconciliation deadline lifecycle contract."""

    kind: Literal["reconciliation_deadline"] = "reconciliation_deadline"
    request_id: RequestId
    now_at: Seconds


class OperationRetireRequested(Value):
    """Explicit retirement of a registered owned operation, resolved by intents."""

    kind: Literal["operation_retire_requested"] = "operation_retire_requested"
    operation: OperationRef
    scope: Scope


class OperationResult(Value):
    """Operation result lifecycle contract."""

    kind: Literal["operation_result"] = "operation_result"
    operation_id: OperationId
    observation: Observation
    outcome_schema: SchemaRef
    operation_schema: OperationSchemaRef
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> OperationResult:
        """Give strategy callbacks the owner's validated model, never an opaque dict."""
        return bind_outcome(self, info)


type IntentsEvent = Annotated[
    RequestPrepared
    | DispatchAuthorized
    | RequestObserved
    | RecoveryStarted
    | ReconciliationDeadline
    | OperationRetireRequested,
    Field(discriminator="kind"),
]
