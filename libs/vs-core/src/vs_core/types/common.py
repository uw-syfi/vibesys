"""Strict immutable values shared by lifecycle areas."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Value(BaseModel):
    """Persistable data, never live interfaces or mutable containers."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


type Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Count = Annotated[int, Field(ge=0)]
type Generation = Annotated[int, Field(ge=0)]
type RevisionNumber = Annotated[int, Field(ge=0)]
type LifecycleCapability = Literal["park", "interrupt", "steer", "suspend"]


class Identity(Value):
    """Canonical tagged identity; domains survive JSON union round trips."""

    root: Annotated[str, Field(min_length=1, pattern=r"^\S+$")]

    @model_validator(mode="before")
    @classmethod
    def distinct(cls, value: object) -> object:
        """Reject a different identity type in Python and tagged JSON."""
        if isinstance(value, Identity) and type(value) is not cls:
            raise IdentityTypeError
        return value


class RunId(Identity):
    """Distinct run identity."""

    kind: Literal["run"] = "run"


class AttemptId(Identity):
    """Distinct attempt identity."""

    kind: Literal["attempt"] = "attempt"


class ItemId(Identity):
    """Distinct item identity."""

    kind: Literal["item"] = "item"


class SessionId(Identity):
    """Distinct session identity."""

    kind: Literal["session"] = "session"


class InvocationId(Identity):
    """Distinct invocation identity."""

    kind: Literal["invocation"] = "invocation"


class ContinuationId(Identity):
    """Distinct continuation identity."""

    kind: Literal["continuation"] = "continuation"


class InputId(Identity):
    """Identity of one input occurrence, independent of content digest."""

    kind: Literal["input"] = "input"


class EvidenceId(Identity):
    """Distinct evidence identity."""

    kind: Literal["evidence"] = "evidence"


class DecisionId(Identity):
    """Distinct decision identity."""

    kind: Literal["decision"] = "decision"


class RequestId(Identity):
    """Distinct request identity."""

    kind: Literal["request"] = "request"


class EvidenceKey(Value):
    """Full identity of one evidence record: the request that produced it plus its ID.

    EvidenceId alone is only unique within one source request, so two jobs can
    report the same ID. Every ledger, settlement and continuation lookup keys on
    this pair.
    """

    kind: Literal["evidence_key"] = "evidence_key"
    source_request: RequestId
    evidence_id: EvidenceId


class EventId(Identity):
    """Distinct event identity."""

    kind: Literal["event"] = "event"


class SettlementId(Identity):
    """Distinct settlement identity."""

    kind: Literal["settlement"] = "settlement"


class StrategyId(Identity):
    """Distinct strategy identity."""

    kind: Literal["strategy"] = "strategy"


class OperationId(Identity):
    """Distinct operation identity."""

    kind: Literal["operation"] = "operation"


class ArtifactId(Identity):
    """Distinct artifact identity."""

    kind: Literal["artifact"] = "artifact"


class ResourceId(Identity):
    """Distinct lease episode, never a reusable raw physical backend identity.

    The owning library's normalization episode-qualifies reused physical IDs;
    conflicting scope or admission claims cannot reuse one core ResourceId.
    """

    kind: Literal["resource"] = "resource"


class RevisionId(Identity):
    """Distinct revision identity."""

    kind: Literal["revision"] = "revision"


class RoleId(Identity):
    """Distinct role identity."""

    kind: Literal["role"] = "role"


class PoolId(Identity):
    """Distinct pool identity."""

    kind: Literal["pool"] = "pool"


class ChargeId(Identity):
    """Distinct charge identity."""

    kind: Literal["charge"] = "charge"


class ControlId(Identity):
    """Distinct control identity."""

    kind: Literal["control"] = "control"


class HostId(Identity):
    """Distinct host identity."""

    kind: Literal["host"] = "host"


class SchemaRef(Value):
    """Schema ref lifecycle contract."""

    name: str = Field(min_length=1)
    version: int = Field(ge=1)


class RevisionRef(Value):
    """Revision ref lifecycle contract."""

    revision_id: RevisionId
    digest: str = Field(min_length=1)


class ArtifactRef(Value):
    """Artifact ref lifecycle contract."""

    artifact_id: ArtifactId
    digest: str = Field(min_length=1)
    schema_ref: SchemaRef | None = None


class Scope(Value):
    """Scope lifecycle contract."""

    owner: Annotated[RunId | AttemptId, Field(discriminator="kind")]
    generation: Generation


class AttemptRef(Value):
    """Attempt ref lifecycle contract."""

    attempt_id: AttemptId
    generation: Generation


class InvocationRef(Value):
    """Invocation ref lifecycle contract."""

    session_id: SessionId
    invocation_id: InvocationId
    generation: Generation


class OperationRef(Value):
    """Operation ref lifecycle contract."""

    operation_id: OperationId
    generation: Generation


class DependencyRef(Value):
    """Dependency ref lifecycle contract."""

    decision_id: DecisionId | None = None
    request_id: RequestId | None = None


class LifecycleClass(StrEnum):
    """Lifecycle class lifecycle contract."""

    QUERY = "query"
    IDEMPOTENT_WRITE = "idempotent_external_write"
    OWNED_JOB = "owned_long_running_job"
    SESSION_TURN = "session_turn"


class EvidenceKind(StrEnum):
    """Closed measurement kinds used for eligibility requirements."""

    CORRECTNESS = "correctness"
    BENCHMARK = "benchmark"
    PROFILING = "profiling"
    LOCAL_VALIDATION = "local-validation"


class AssessmentKind(StrEnum):
    """Closed semantic assessment kinds, distinct from evidence production."""

    CORRECTNESS = "correctness"
    BENCHMARK = "benchmark"
    PROFILING = "profiling"
    LOCAL_VALIDATION = "local-validation"


class ObservationStatus(StrEnum):
    """Observation status lifecycle contract."""

    SUCCEEDED = "succeeded"
    PENDING = "pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    """Run status lifecycle contract."""

    RUNNING = "running"
    PAUSED = "paused"
    CLOSING = "closing"
    TERMINAL = "terminal"
    BLOCKED = "blocked"


class RejectionCode(StrEnum):
    """Rejection code lifecycle contract."""

    STALE_VIEW = "stale-view"
    IDENTITY_CONFLICT = "identity-conflict"
    UNKNOWN_KIND = "unknown-kind"
    UNKNOWN_SCHEMA = "unknown-schema"
    UNDECLARED_OPERATION = "undeclared-operation"
    CAPABILITY = "capability"
    OWNERSHIP = "ownership"
    GENERATION = "generation"
    CLOSED_SCOPE = "closed-scope"
    BUDGET = "budget"
    DEPENDENCY = "dependency"
    EVIDENCE = "evidence"
    ALREADY_SETTLED = "already-settled"
    NOT_IMPLEMENTED_IN_KERNEL = "not-implemented-in-kernel"


class Area(StrEnum):
    """Area lifecycle contract."""

    SCHEDULING = "scheduling"
    ATTEMPTS = "attempts"
    SESSIONS = "sessions"
    EVALUATION = "evaluation"
    SETTLEMENT = "settlement"
    INTENTS = "intents"


class KernelNotImplementedError(Exception):
    """Typed wave-1 rejection; callers may identify the exact owning lane."""

    code = RejectionCode.NOT_IMPLEMENTED_IN_KERNEL

    def __init__(self, area: Area, event_kind: str, subarea: str | None = None) -> None:
        """Name the owning area and its unimplemented event."""
        self.area = area
        self.event_kind = event_kind
        self.subarea = subarea
        owner = f"{area.value}/{subarea}" if subarea is not None else area.value
        super().__init__(f"{owner}: {event_kind} not implemented in kernel")


class SignalCycleError(Exception):
    """A reducer re-emitted a signal already consumed in this transition."""


class OperationSchemaRef(Value):
    """Operation schema ref lifecycle contract."""

    kind: str = Field(min_length=1)
    request_schema: SchemaRef
    outcome_schema: SchemaRef
    lifecycle: LifecycleClass


class RevisionAuthority(StrEnum):
    """Explicit workspace authority, never inferred from operation kind names."""

    NONE = "none"
    SNAPSHOT = "snapshot"
    RETAIN = "retain"
    RESTORE = "restore"
    DISCARD = "discard"


class OperationNormalizationKind(StrEnum):
    """Closed normalization routes; pure normalizers live outside state."""

    NONE = "none"
    SCOPE_REOPEN = "scope_reopen"


class ScopeReopenNormalization(Value):
    """Canonical guarded reopen target bound to a registered payload.

    The operation is run-scoped. Its target must be the exact parked attempt,
    and resolved_cancelled_jobs must equal the continuation cancellation set.
    A newer park authority invalidates older reopen acknowledgements.
    """

    kind: Literal["scope_reopen"] = "scope_reopen"
    attempt: AttemptRef
    continuation_id: ContinuationId
    park_authority: RequestId
    resolved_cancelled_jobs: tuple[ResourceId, ...]

    @model_validator(mode="after")
    def unique_resolutions(self) -> ScopeReopenNormalization:
        """Reject duplicate cancellation-resolution claims at ingress."""
        if len(set(self.resolved_cancelled_jobs)) != len(self.resolved_cancelled_jobs):
            raise ContractValidationError("resolved_cancelled_jobs", "duplicate resource ID")
        return self


class OperationDescriptor(OperationSchemaRef):
    """Operation descriptor lifecycle contract."""

    normalization: OperationNormalizationKind = OperationNormalizationKind.NONE
    inspect: bool = False
    cancel: bool = False
    watch: bool = False
    resource_pool: PoolId | None = None
    revision_authority: RevisionAuthority = RevisionAuthority.NONE

    @model_validator(mode="after")
    def lifecycle_contract(self) -> OperationDescriptor:
        """Require explicit resource and revision ownership declarations."""
        if self.lifecycle == LifecycleClass.OWNED_JOB and self.resource_pool is None:
            raise OperationDescriptorError("resource_pool", "owned jobs require a declared pool")
        if self.lifecycle != LifecycleClass.OWNED_JOB and self.resource_pool is not None:
            raise OperationDescriptorError("resource_pool", "only owned jobs declare pools")
        if (
            self.revision_authority != RevisionAuthority.NONE
            and self.lifecycle != LifecycleClass.IDEMPOTENT_WRITE
        ):
            raise OperationDescriptorError(
                "revision_authority", "revision mutations require idempotent writes"
            )
        if self.normalization == OperationNormalizationKind.SCOPE_REOPEN and (
            self.lifecycle != LifecycleClass.IDEMPOTENT_WRITE
            or not self.inspect
            or self.resource_pool is not None
            or self.revision_authority != RevisionAuthority.NONE
        ):
            raise OperationDescriptorError(
                "normalization", "scope reopen requires inspectable non-revision idempotent write"
            )
        return self


class ContractValidationError(ValueError):
    """Invalid immutable value, identifying the exact field and violated contract."""

    def __init__(self, path: str, detail: str) -> None:
        """Name the offending field without masking a plausible default."""
        super().__init__(f"{path}: {detail}")


class OperationDescriptorError(ValueError):
    """Invalid operation ownership contract."""

    def __init__(self, path: str, detail: str) -> None:
        """Name the invalid declaration."""
        super().__init__(f"{path}: {detail}")


class Capabilities(Value):
    """Capabilities lifecycle contract."""

    lifecycle: frozenset[LifecycleCapability] = frozenset()
    operations: tuple[OperationDescriptor, ...] = ()


class PoolCapacity(Value):
    """Scheduling-owned bound for one named capacity pool.

    Unconfigured pools retain exclusive capacity one. Global max_parallel is
    always an additional bound; a pool capacity never grants unbounded slots.
    """

    pool_id: PoolId
    capacity: int = Field(ge=1)


class Limits(Value):
    """Run bounds; Scheduling enforces capacities before acquiring an episode."""

    max_attempts: Count = 1
    max_turns: Count = 1
    max_parallel: int = Field(default=1, ge=1)
    pool_capacities: tuple[PoolCapacity, ...] = ()
    max_retries: Count = 0
    max_refunds: Count = 0
    max_measurement_submissions: Count = 3
    queue_allowance: Seconds = 900.0
    cancellation_bound: Seconds = 60.0
    reconciliation_bound: Seconds = 60.0

    @model_validator(mode="after")
    def distinct_pool_capacities(self) -> Limits:
        """Reject contradictory capacity declarations for the same pool."""
        identities = tuple(pool.pool_id for pool in self.pool_capacities)
        if len(set(identities)) != len(identities):
            raise ContractValidationError("pool_capacities", "duplicate pool_id")
        return self


class PureOption(Value):
    """Pure option lifecycle contract."""

    key: str = Field(min_length=1)
    value: str | int | Annotated[float, Field(allow_inf_nan=False)] | bool | None


class RunFacts(Value):
    """Run facts lifecycle contract."""

    objective: str
    options: tuple[PureOption, ...] = ()
    constraints: tuple[PureOption, ...] = ()
    baseline: RevisionRef
    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    artifacts: tuple[ArtifactRef, ...] = ()


class ControlInput(Value):
    """Control input lifecycle contract."""

    control_id: ControlId
    action: Literal["pause", "resume", "stop", "steer"]
    artifact: ArtifactRef | None = None


class CompletionStatus(StrEnum):
    """Terminal semantic decision outcome, independent of dispatch acceptance."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DependencyStatus(StrEnum):
    """Readiness of durable completion and request dependencies."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RequestBase(Value):
    """Canonical I/O intent correlation, including the current occupancy episode.

    Attempt mutations carry admission_id. Canonical registration derives a
    missing attempt episode from the exact owner and generation. Explicit older
    episodes authorize only recorded cleanup, never mutation of a newer episode.
    Delayed observations may complete that cleanup but cannot mutate later
    ownership. Referenced IDs are allocated before state or signals refer to them.
    """

    request_id: RequestId | None = None
    scope: Scope
    depends_on: tuple[RequestId, ...] = ()
    decision_id: DecisionId | None = None
    admission_id: DecisionId | None = None
    decision_dependencies: tuple[DecisionId, ...] = ()
    deadline_at: Seconds


class Observation(Value):
    """External facts correlated by request, scope, generation and episode.

    children_complete explicitly claims an authoritative manifest; an empty
    children tuple alone does not prove it. Install discovered child ownership
    before removing provisional request ownership. Unknown acceptance or missing
    identity never proves release. revision is the revision a snapshot or retain
    request produced or retained, as the executor saw it; a caller-supplied
    revision on a derived event is accepted only when it equals this one.
    """

    event_id: EventId
    request_id: RequestId
    scope: Scope
    sequence: Count
    observed_at: Seconds
    status: ObservationStatus
    resource_id: ResourceId | None = None
    revision: RevisionRef | None = None
    accepted: bool = False
    terminal: bool = False
    released: bool = False
    children: tuple[ResourceId, ...] = ()
    children_complete: bool = False
    admission_id: DecisionId | None = None
    diagnostic: str = ""


class ChargeKind(StrEnum):
    """Separate currencies with different admission and refund semantics.

    ADMISSION charges accepted registration including queued starts. ATTEMPT
    charges a paid invocation or failed setup cycle without an existing paid
    charge. TURN charges one logical invocation, regardless of max_turns or
    charge class, and never refunds.
    """

    ADMISSION = "admission"
    ATTEMPT = "attempt"
    TURN = "turn"


class ChargeReceipt(Value):
    """Authoritative charge and bounded, deduplicated refund proof.

    Historical aggregate proof preserves consumed migration budget only. It
    grants no dispatch authority and cannot receive a new refund without exact
    live charge correspondence. Unsupported ADMISSION refunds require positive
    nonacceptance and drained ownership; ATTEMPT refunds need bounded authority.
    """

    charge_id: ChargeId
    kind: ChargeKind
    invocation_id: InvocationId | None = None
    source_request: RequestId | None = None
    charged: Count
    refunded: Count = 0
    refund_sources: tuple[RequestId, ...] = ()
    historical_proof: ArtifactRef | None = None

    @model_validator(mode="after")
    def bounded_refund(self) -> ChargeReceipt:
        """Reject over-refunds, repeated authority and any TURN refund."""
        if self.refunded > self.charged:
            raise ContractValidationError("refunded", "exceeds charged amount")
        if self.kind == ChargeKind.TURN and self.refunded:
            raise ContractValidationError("refunded", "TURN charges never refund")
        if len(set(self.refund_sources)) != len(self.refund_sources):
            raise ContractValidationError("refund_sources", "duplicate refund authority")
        return self


class SetupFailureKind(StrEnum):
    """Setup classification; unknown acceptance requires inspection."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


def validate_setup_failure(observation: Observation, failure: SetupFailureKind) -> None:
    """Classification requires terminal failure and grants no acceptance proof.

    An accepted-but-failed setup retains its lease cleanup obligations. Refunds
    require separate positive nonacceptance and recorded charge authority.
    """
    if failure != SetupFailureKind.UNKNOWN and not (
        observation.terminal
        and observation.status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        raise ContractValidationError("setup_failure", "requires terminal setup failure")


class HostFence(Value):
    """Host fence lifecycle contract."""

    host_id: HostId
    epoch: Count


class EventCursor(Value):
    """Event cursor lifecycle contract."""

    sequence: Count


class IdentityTypeError(ValueError):
    """Different identity domains cannot be substituted."""

    def __init__(self) -> None:
        """Report a domain mismatch at validation ingress."""
        super().__init__("identity type mismatch")


class WorkspaceMode(StrEnum):
    """Workspace mode lifecycle contract."""

    EXCLUSIVE_ROOT = "exclusive-root"
    ISOLATED_CHILD = "isolated-child"
    READ_ONLY_REVISION = "read-only-revision"


class WorkspaceRef(Value):
    """Workspace ref lifecycle contract."""

    scope: Scope
    revision: RevisionRef
    mode: WorkspaceMode


class ReleaseDependency(Value):
    """An owned resource or disposition acknowledgement required for release."""

    kind: Literal["session", "job", "workspace", "operation", "request"]
    identity: SessionId | ResourceId | RequestId | OperationId

    @model_validator(mode="after")
    def domain_matches_kind(self) -> ReleaseDependency:
        """Reject accidental cross-domain release graph edges."""
        expected = {
            "session": SessionId,
            "job": ResourceId,
            "workspace": RequestId,
            "operation": OperationId,
            "request": RequestId,
        }
        if type(self.identity) is not expected[self.kind]:
            raise IdentityTypeError
        return self


class OperationWire(Value):
    """Operation wire lifecycle contract."""

    schema_ref: OperationSchemaRef
    payload_json: str


class ReservedInputOccurrence(Value):
    """Immutable execution payload for an input occurrence reserved by Sessions B.

    Sessions A validates the exact ID/artifact manifest against InputRecord before
    dispatch. The operation wire remains canonical and is never rewritten to
    transport inputs. Equal artifacts with distinct occurrence IDs stay distinct.
    """

    input_id: InputId
    artifact: ArtifactRef


class ExecuteRegisteredOperation(RequestBase):
    """Execute a canonical registered wire with separately reserved input payload.

    Sessions A resolves the accepted Operation origin and reserves its inputs
    before Intents A registers this final executable payload. Dispatch uses that
    sole canonical manifest; changing it under the same request ID is a conflict.
    """

    kind: Literal["execute_registered_operation"] = "execute_registered_operation"
    operation_id: OperationId
    operation: OperationWire
    retry_limit: Count
    inputs: tuple[ReservedInputOccurrence, ...] = ()

    @model_validator(mode="after")
    def distinct_reserved_occurrences(self) -> ExecuteRegisteredOperation:
        """An occurrence may appear once even when artifacts repeat."""
        if self.inputs and self.operation.schema_ref.lifecycle != LifecycleClass.SESSION_TURN:
            raise ContractValidationError(
                "inputs", "reserved inputs require SESSION_TURN ownership"
            )
        identities = tuple(item.input_id for item in self.inputs)
        if len(set(identities)) != len(identities):
            raise ContractValidationError("inputs", "duplicate input_id")
        return self
