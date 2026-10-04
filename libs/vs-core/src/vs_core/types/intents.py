"""Authoritative outbox and class-driven operation observation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, Field, SerializeAsAny, ValidationInfo, model_validator

from vs_core._outcomes import OutcomeCodecError, OutcomeValue, bind_outcome

from .attempts import (
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    RestoreRevision,
    RetainRevision,
    SnapshotAndRetain,
    WorkspaceRequest,
)
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
    SetupFailureKind,
    Value,
    validate_setup_failure,
)
from .evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    Continuation,
    EvaluationRequest,
    EvidenceRef,
    InspectOwnedJob,
    MeasurementIdentity,
    ObserveOwnedJob,
    SubmitMeasurement,
)
from .evaluation_history import EvaluationTerminalFacts
from .job_observations import JobProgress, MeasurementFailure
from .sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    ResumeSessionTurn,
    SessionRequest,
    SnapshotAndRetainRun,
    TurnSpec,
)
from .settlement import AdoptionRequest, AdoptRevision, VerifyAdoption


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


class IntentBlocked(Value):
    """Diagnostic that reconciliation blocked one intent, published once.

    request_id is the BlockIntent request, target the blocked intent. Emitted when
    the block is first prepared, never on replay, so hosts and strategies can
    surface it without reading the intent ledger.
    """

    kind: Literal["intent_blocked"] = "intent_blocked"
    request_id: RequestId
    target: RequestId
    scope: Scope
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
    """Canonical outcome ledger including immutable typed setup classification.

    Intents A retains root/target setup_failure with the corresponding observation
    and checks equality on replay before forwarding SessionObserved.failure.
    UNKNOWN never supplies nonacceptance, unsupported refund or success proof.
    evaluation_result retains the exact scientific ingress facts alongside its
    observation for same-sequence equality; Intents A forwards them unchanged.
    """

    request_id: RequestId
    request: Request
    payload_digest: str
    lifecycle: LifecycleClass
    phase: IntentPhase
    sequence: Count | None = None
    observation: Observation | None = None
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN
    evaluation_result: EvaluationTerminalFacts | None = None
    suspension: Continuation | None = None
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    retry_count: Count = 0
    reconcile_deadline_at: Seconds

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> Intent:
        """Restore the owning subtype at the registered codec boundary."""
        if self.observation is None:
            if self.setup_failure != SetupFailureKind.UNKNOWN:
                raise ContractValidationError(
                    "setup_failure", "requires terminal setup observation"
                )
        else:
            validate_setup_failure(self.observation, self.setup_failure)
        if self.evaluation_result is not None:
            if self.observation is None:
                raise ContractValidationError(
                    "evaluation_result", "requires its accepted terminal observation"
                )
            self.evaluation_result.validate_observation(self.observation)
        return bind_outcome(self, info)


class ChildObservationWatermark(Value):
    """Highest accepted observation from one child-discovery source.

    The full immutable observation carries sequence and equality proof. Intents
    B rejects an older sequence or conflicting facts at the same sequence before
    changing aggregate ownership, even when another source supplies that proof.
    """

    source_request: RequestId
    observation: Observation

    @model_validator(mode="after")
    def exact_source(self) -> ChildObservationWatermark:
        """A watermark belongs only to its observation's canonical source."""
        if self.source_request != self.observation.request_id:
            raise ContractValidationError("source_request", "differs from observation request")
        return self


class ChildLease(Value):
    """Discovered descendant ownership, distinct from its ancestor's intent.

    Source requests and parent resources are unique, canonically ordered proofs.
    Conflicting scope or ancestry rejects; transfer to a typed owner is atomic.
    Intents B maintains watermarks independently of aggregate observation.
    Incomplete historical watermarks cannot authorize release or transfer;
    reconcile every source before marking the history complete.
    Retained source claims conservatively retain independent ownership: run
    finality requires conclusive release from every source and preserves every
    retained source's descendant manifest. The aggregate observation cannot
    erase those claims. No alternative-reporter authority is offered.
    """

    resource_id: ResourceId
    scope: Scope
    source_requests: tuple[RequestId, ...] = Field(min_length=1)
    parent_resources: tuple[ResourceId, ...] = ()
    observation: Observation | None = None
    observation_watermarks: tuple[ChildObservationWatermark, ...] = ()
    watermark_history_complete: bool = False

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
        self._validate_watermarks()
        return self

    def _validate_watermarks(self) -> None:
        """Incomplete migrated history grants no stale-source release authority."""
        sources = tuple(mark.source_request.root for mark in self.observation_watermarks)
        if sources != tuple(sorted(set(sources))):
            raise ContractValidationError(
                "observation_watermarks", "unique canonically ordered sources required"
            )
        for mark in self.observation_watermarks:
            if (
                mark.source_request not in self.source_requests
                or mark.observation.scope != self.scope
                or mark.observation.resource_id != self.resource_id
            ):
                raise ContractValidationError(
                    "observation_watermarks", "source, scope and child resource must match lease"
                )
        if self.watermark_history_complete:
            if set(sources) != {source.root for source in self.source_requests}:
                raise ContractValidationError(
                    "watermark_history_complete", "every source requires its watermark"
                )
            if self.observation is not None and not any(
                mark.observation == self.observation for mark in self.observation_watermarks
            ):
                raise ContractValidationError(
                    "observation", "aggregate proof must match an accepted source watermark"
                )


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
    """Canonical proposal before an executable intent acquires final payload.

    For registered session turns, Intents A delegates acquisition and input
    reservation first. Sessions A validates the accepted Operation receipt and
    normalized turn, then returns the same ExecuteRegisteredOperation identity
    with its fixed occurrence manifest. Only that final payload is registered
    and persisted before dispatch. A proposal with empty inputs is never proof
    that reservation completed or authority to dispatch a later changed payload.
    normalized_measurement is the owning registration's canonical expectation;
    absence never derives measurement identity from observed evidence.
    """

    kind: Literal["request_prepared"] = "request_prepared"
    request: Request
    lifecycle: LifecycleClass
    normalized_turn: TurnSpec | None = None
    normalized_measurement: MeasurementIdentity | None = None
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
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN
    target_resource: ResourceId | None = None
    revision: RevisionRef | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    progress: JobProgress | None = None
    measurement_failure: MeasurementFailure | None = None
    evaluation_result: EvaluationTerminalFacts | None = None
    suspension: Continuation | None = None
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    operation_schema: OperationSchemaRef | None = None

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> TargetObservation:
        """Restore registered target subtypes at the owning codec boundary."""
        validate_setup_failure(self.observation, self.setup_failure)
        if self.evaluation_result is not None:
            self.evaluation_result.validate_observation(self.observation)
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
    """Root request outcome with executor-owned typed setup failure.

    Inspection's setup classification belongs in target.setup_failure, separately
    from the query outcome. Intents A forwards the exact corresponding value to
    Sessions A and preserves it in the canonical ledger for equality on replay.
    """

    kind: Literal["request_observed"] = "request_observed"
    target: TargetObservation | None = None
    progress: JobProgress | None = None
    measurement_failure: MeasurementFailure | None = None
    evaluation_result: EvaluationTerminalFacts | None = None
    suspension: Continuation | None = None
    observation: Observation
    setup_failure: SetupFailureKind = SetupFailureKind.UNKNOWN
    outcome_schema: SchemaRef | None = None
    outcome_json: str | None = None
    outcome: SerializeAsAny[BaseModel] | None = Field(default=None, exclude=True)
    evidence: tuple[EvidenceRef, ...] = ()
    revision: RevisionRef | None = None
    operation_schema: OperationSchemaRef | None = None

    @model_validator(mode="after")
    def registered_outcome(self, info: ValidationInfo) -> RequestObserved:
        """Restore the owning subtype at the registered codec boundary."""
        validate_setup_failure(self.observation, self.setup_failure)
        if self.evaluation_result is not None:
            self.evaluation_result.validate_observation(self.observation)
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


def request_lifecycle(request: Request) -> LifecycleClass:
    """Classify the canonical request without reducer or backend knowledge."""
    match request:
        case ExecuteRegisteredOperation():
            lifecycle = request.operation.schema_ref.lifecycle
        case DispatchTurn() | ResumeSessionTurn():
            lifecycle = LifecycleClass.SESSION_TURN
        case SubmitMeasurement():
            lifecycle = LifecycleClass.OWNED_JOB
        case (
            InspectTurn()
            | ObserveOwnedJob()
            | InspectOwnedJob()
            | CollectEvidence()
            | InspectRequest()
            | VerifyAdoption()
        ):
            lifecycle = LifecycleClass.QUERY
        case (
            EnsureWorkspace()
            | RestoreRevision()
            | SnapshotAndRetain()
            | SnapshotAndRetainRun()
            | RetainRevision()
            | DiscardWorkspace()
            | CloseAttemptScope()
            | EnsureSession()
            | CancelTurn()
            | CloseSession()
            | CancelOwnedJob()
            | AdoptRevision()
            | CancelOwnedResource()
            | BlockIntent()
        ):
            lifecycle = LifecycleClass.IDEMPOTENT_WRITE
        case _:
            assert_never(request)
    return lifecycle
