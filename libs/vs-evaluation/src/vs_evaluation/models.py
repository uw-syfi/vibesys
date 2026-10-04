"""Typed values for durable, provider-neutral evaluation execution."""

# TRY003 is suppressed on Pydantic validators below: Pydantic uses ValueError
# text as the field-scoped boundary diagnostic. A custom exception per model
# invariant or a generic message factory would hide which invariant failed.

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    field_validator,
    model_validator,
)


class EvaluationState(StrEnum):
    """Persisted lifecycle state for an entire evaluation request."""

    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


EvaluationStatus = EvaluationState


class EvaluationLifecyclePhase(StrEnum):
    """Semantic reason one evaluation lifecycle observation was published."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"
    TIMED_OUT = "timed_out"


class StageState(StrEnum):
    """Terminal state for one named step in an evaluation plan."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"


class StageFailureKind(StrEnum):
    """Whether execution failed or its evidence failed after execution."""

    EXECUTION = "execution"
    COLLECTION = "collection"


class AvailabilityState(StrEnum):
    """Normalized availability independent of the execution provider."""

    IMMEDIATE = "immediate"
    BUSY = "busy"
    DELAYED = "delayed"
    UNAVAILABLE = "unavailable"


class ReuseStatus(StrEnum):
    """Whether matching reusable work is already known to the executor."""

    AVAILABLE = "available"
    NONE = "none"
    UNKNOWN = "unknown"


class CostClass(StrEnum):
    """Coarse provider-neutral execution cost classification."""

    FREE = "free"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class ResourceRequirements(BaseModel):
    """Generic resource quantities requested by an evaluation or stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cpu_cores: FiniteFloat | None = Field(default=None, gt=0)
    memory_bytes: Annotated[int, Field(gt=0)] | None = None
    wall_time_seconds: Annotated[int, Field(gt=0)] | None = None
    capabilities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _capability_names_are_valid(self) -> ResourceRequirements:
        if any(not name or name.strip() != name for name in self.capabilities):
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930017 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "capabilities must contain non-empty, trimmed names"
            )
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must not contain duplicates")  # noqa: TRY003  # lint-waiver: LW-930018 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self


class EvaluationStep(BaseModel):
    """One ordered named stage and its provider-neutral input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    payload: JsonValue
    requirements: ResourceRequirements = Field(default_factory=ResourceRequirements)

    @model_validator(mode="after")
    def _name_is_trimmed(self) -> EvaluationStep:
        if self.name.strip() != self.name:
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930019 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "stage name must not contain leading or trailing whitespace"
            )
        return self


class EvaluationRequest(BaseModel):
    """Immutable ordered plan plus a stable caller-supplied idempotency key.

    The library treats each step payload as opaque JSON. Domain trust and
    interpretation belong to the caller that constructs and consumes it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str = Field(min_length=1)
    stages: tuple[EvaluationStep, ...] = Field(min_length=1)
    stop_on_failure: bool = True
    requirements: ResourceRequirements = Field(default_factory=ResourceRequirements)

    @model_validator(mode="after")
    def _request_identity_is_valid(self) -> EvaluationRequest:
        if self.key.strip() != self.key:
            raise ValueError("key must not contain leading or trailing whitespace")  # noqa: TRY003  # lint-waiver: LW-930020 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("stage names must be unique within a request")  # noqa: TRY003  # lint-waiver: LW-930021 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self


class AvailabilitySnapshot(BaseModel):
    """One timestamped observation of matching capacity and estimates."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: AvailabilityState
    capacity: Annotated[int, Field(ge=0)] | None = None
    in_flight: Annotated[int, Field(ge=0)]
    queue_depth: Annotated[int, Field(ge=0)]
    estimated_start_after_s: FiniteFloat | None = Field(default=None, ge=0)
    estimated_runtime_s: FiniteFloat | None = Field(default=None, ge=0)
    reuse_status: ReuseStatus
    cost_class: CostClass
    observed_at: FiniteFloat
    fresh_for_s: FiniteFloat = Field(gt=0)
    supported_evidence_kinds: tuple[str, ...] = ()
    supported_capabilities: tuple[str, ...] = ()

    def is_fresh(self, now: float) -> bool:
        """Return whether this observation remains within its declared age."""
        return 0 <= now - self.observed_at <= self.fresh_for_s


class EvaluationStepResult(BaseModel):
    """Terminal result and elapsed duration for one planned stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    state: StageState
    result: JsonValue | None = None
    failure: str | None = None
    failure_kind: StageFailureKind | None = None
    """Absent legacy provenance retains execution stop-on-failure semantics."""
    duration_s: FiniteFloat | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _result_matches_state(self) -> EvaluationStepResult:
        if self.state is StageState.FAILED and not self.failure:
            raise ValueError("failed stage requires failure")  # noqa: TRY003  # lint-waiver: LW-930022 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if self.state is not StageState.FAILED and (
            self.failure is not None or self.failure_kind is not None
        ):
            raise ValueError("only failed stage may include failure")  # noqa: TRY003  # lint-waiver: LW-930023 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self


class ExecutorObservation(BaseModel):
    """One executor snapshot used by the coordinator for reconciliation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    state: EvaluationState
    current_stage: str | None = None
    stage_results: tuple[EvaluationStepResult, ...] = ()
    failure: str | None = None

    @model_validator(mode="after")
    def _terminal_failure_matches_state(self) -> ExecutorObservation:
        if self.state is EvaluationState.FAILED and not self.failure:
            raise ValueError("failed evaluation requires failure")  # noqa: TRY003  # lint-waiver: LW-930024 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if self.state is not EvaluationState.FAILED and self.failure is not None:
            raise ValueError("only failed evaluation may include failure")  # noqa: TRY003  # lint-waiver: LW-930025 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if (
            self.state in {EvaluationState.SUCCEEDED, EvaluationState.FAILED}
            and self.current_stage is not None
        ):
            raise ValueError("terminal evaluation cannot have current_stage")  # noqa: TRY003  # lint-waiver: LW-930026 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self


class StoredEvaluation(BaseModel):
    """Authoritative durable lifecycle record owned by an EvaluationStore."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    handle_id: str = Field(min_length=1)
    request: EvaluationRequest
    state: EvaluationState
    revision: Annotated[int, Field(ge=0)]
    current_stage: str | None = None
    stage_results: tuple[EvaluationStepResult, ...] = ()
    failure: str | None = None
    submission_pending: bool = False
    cancel_requested: bool = False

    @property
    def status(self) -> EvaluationStatus:
        """Return the caller-facing lifecycle status."""
        return EvaluationStatus(self.state.value)


class EvaluationLifecycleEvent(BaseModel):
    """One revisioned lifecycle observation from the coordinator.

    Durable record revisions are authoritative. ``TIMED_OUT`` describes a
    bounded caller wait and may lack a revision when the initial store read
    was interrupted. Sinks can receive duplicates during reconciliation, so
    consumers deduplicate by handle, phase, and revision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["evaluation_lifecycle"] = "evaluation_lifecycle"
    phase: EvaluationLifecyclePhase
    handle_id: str = Field(min_length=1)
    scope_id: str | None = None
    revision: Annotated[int, Field(ge=0)] | None = None
    state: EvaluationState | None = None
    current_stage: str | None = None
    stage_results: tuple[EvaluationStepResult, ...] = ()
    failure: str | None = None
    submission_pending: bool = False
    cancel_requested: bool = False

    @field_validator("stage_results", mode="before")
    @classmethod
    def _stage_results_from_json(cls, value: object) -> object:
        """Accept the JSON array representation while keeping immutable tuples."""
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _phase_matches_snapshot(self) -> EvaluationLifecycleEvent:
        if self.phase is EvaluationLifecyclePhase.TIMED_OUT:
            return self
        if self.revision is None or self.state is None:
            raise ValueError("durable lifecycle event requires revision and state")  # noqa: TRY003  # lint-waiver: LW-930027 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if self.phase is EvaluationLifecyclePhase.SUBMITTED:
            if self.state is not EvaluationState.QUEUED or not self.submission_pending:
                raise ValueError("submitted event requires a pending queued record")  # noqa: TRY003  # lint-waiver: LW-930028 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
            return self
        if self.phase.value != self.state.value or self.submission_pending:
            raise ValueError("lifecycle phase must match its durable state")  # noqa: TRY003  # lint-waiver: LW-930029 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self


class EvaluationCompleted(BaseModel):
    """Successful bounded await result with each stage's evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["completed"] = "completed"
    handle_id: str
    stages: tuple[EvaluationStepResult, ...]


class EvaluationTimedOut(BaseModel):
    """Bounded wait expired; status is absent if no durable read completed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["timed_out"] = "timed_out"
    handle_id: str
    status: EvaluationStatus | None


class EvaluationFailed(BaseModel):
    """Execution failed before producing an accepted result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["failed"] = "failed"
    handle_id: str
    message: str


class EvaluationCanceled(BaseModel):
    """Evaluation was canceled or superseded before successful completion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["canceled"] = "canceled"
    handle_id: str
    state: Literal[EvaluationState.CANCELED, EvaluationState.SUPERSEDED]


EvaluationAwaitResult = Annotated[
    EvaluationCompleted | EvaluationTimedOut | EvaluationFailed | EvaluationCanceled,
    Field(discriminator="outcome"),
]
