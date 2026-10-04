"""Strict immutable values shared by lifecycle areas."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Value(BaseModel):
    """Persistable data, never live interfaces or mutable containers."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


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


class EvidenceId(Identity):
    """Distinct evidence identity."""

    kind: Literal["evidence"] = "evidence"


class DecisionId(Identity):
    """Distinct decision identity."""

    kind: Literal["decision"] = "decision"


class RequestId(Identity):
    """Distinct request identity."""

    kind: Literal["request"] = "request"


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
    """Distinct resource identity."""

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

    def __init__(self, area: Area, event_kind: str) -> None:
        """Name the owning area and its unimplemented event."""
        self.area = area
        self.event_kind = event_kind
        super().__init__(f"{area.value}: {event_kind} not implemented in kernel")


class SignalCycleError(Exception):
    """A reducer re-emitted a signal already consumed in this transition."""


class OperationSchemaRef(Value):
    """Operation schema ref lifecycle contract."""

    kind: str = Field(min_length=1)
    request_schema: SchemaRef
    outcome_schema: SchemaRef
    lifecycle: LifecycleClass


class OperationDescriptor(OperationSchemaRef):
    """Operation descriptor lifecycle contract."""

    inspect: bool = False
    cancel: bool = False
    watch: bool = False


class Capabilities(Value):
    """Capabilities lifecycle contract."""

    lifecycle: frozenset[LifecycleCapability] = frozenset()
    operations: tuple[OperationDescriptor, ...] = ()


class Limits(Value):
    """Limits lifecycle contract."""

    max_attempts: Count = 1
    max_turns: Count = 1
    max_parallel: int = Field(default=1, ge=1)
    max_retries: Count = 0
    max_refunds: Count = 0
    queue_allowance: Seconds = 900.0
    cancellation_bound: Seconds = 60.0
    reconciliation_bound: Seconds = 60.0


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
    """Request base lifecycle contract."""

    request_id: RequestId | None = None
    scope: Scope
    depends_on: tuple[RequestId, ...] = ()
    decision_id: DecisionId | None = None
    decision_dependencies: tuple[DecisionId, ...] = ()
    deadline_at: Seconds


class Observation(Value):
    """Observation lifecycle contract."""

    event_id: EventId
    request_id: RequestId
    scope: Scope
    sequence: Count
    observed_at: Seconds
    status: ObservationStatus
    resource_id: ResourceId | None = None
    accepted: bool = False
    terminal: bool = False
    released: bool = False
    children: tuple[ResourceId, ...] = ()
    diagnostic: str = ""


class ChargeReceipt(Value):
    """Charge receipt lifecycle contract."""

    charge_id: ChargeId
    invocation_id: InvocationId | None = None
    charged: Count
    refunded: Count = 0


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

    kind: Literal["session", "job", "workspace", "operation"]
    identity: SessionId | ResourceId | RequestId | OperationId

    @model_validator(mode="after")
    def domain_matches_kind(self) -> ReleaseDependency:
        """Reject accidental cross-domain release graph edges."""
        expected = {
            "session": SessionId,
            "job": ResourceId,
            "workspace": RequestId,
            "operation": OperationId,
        }
        if type(self.identity) is not expected[self.kind]:
            raise IdentityTypeError
        return self
