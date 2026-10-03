"""Strict contracts for the agent evaluation socket boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, model_validator

from vs_evaluation.agent_evidence import (
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    PartialMeasurement,
    TrustedEvidence,
)
from vs_evaluation.models import (
    AvailabilitySnapshot,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationState,
)
from vs_evaluation.profiler_models import (
    MAX_AGENT_AWAIT_S,
    AwaitProfilerCall,
    CancelProfilerCall,
    DispatchProfilerCall,
    ProfilerAwaitReply,
    ProfilerCanceledReply,
    ProfilerDispatchedReply,
    ProfilerOperationsCall,
    ProfilerOperationsReply,
    ProfilerRunObservation,
    ProfilerStatusCall,
    ProfilerStatusReply,
)


class EvaluationAgentRole(StrEnum):
    """Closed capability profiles exposed to optimization agents."""

    IMPLEMENTER = "implementer"
    PROFILER = "profiler"
    JUDGE = "judge"
    ORCHESTRATOR = "orchestrator"
    PORTFOLIO_DISPATCH = "portfolio_dispatch"
    RUN_OBSERVER = "run_observer"


class EvidencePreflightResolution(StrEnum):
    """Framework conclusion for one declared evidence prerequisite."""

    ACCEPTED = "accepted"
    COLLECTABLE = "collectable"
    UNAUTHORIZED = "unauthorized"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class EvidencePreflightCheck(BaseModel):
    """Deterministic resolution of one evidence prerequisite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_kind: EvidenceKind
    resolution: EvidencePreflightResolution


class EvidencePreflightDecision(BaseModel):
    """Whether framework evidence policy permits a paid role turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    blocked: bool
    checks: tuple[EvidencePreflightCheck, ...]


class EvaluationGrant(BaseModel):
    """Host-created authority for one role and candidate workspace scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str = Field(min_length=32)
    principal_id: str = Field(min_length=1)
    role: EvaluationAgentRole
    scope_id: str | None = None
    profiler_available: bool = False
    run_observer: bool = False


class SubmittedSemanticEvaluation(BaseModel):
    """Host-built immutable candidate identity and its durable execution handle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle_id: str = Field(min_length=1)
    fingerprints: EvidenceFingerprints


class HandleAccess(BaseModel):
    """Durable ownership and observation rights for one opaque handle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle_id: str = Field(min_length=1)
    scope_id: str | None = None
    fingerprints: EvidenceFingerprints
    kinds: tuple[EvidenceKind, ...]
    observers: frozenset[str]
    owners: frozenset[str]


class EvaluationAgentState(BaseModel):
    """Project-owned durable access records. Bearer tokens are never persisted."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    handles: tuple[HandleAccess, ...] = ()

    @model_validator(mode="after")
    def _unique_handles(self) -> EvaluationAgentState:
        ids = [item.handle_id for item in self.handles]
        if len(ids) != len(set(ids)):
            message = "evaluation access handles must be unique"
            raise ValueError(message)
        return self


class AvailabilityCall(BaseModel):
    """Ask for a normalized resource observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["availability"] = "availability"
    token: str
    evidence_kinds: tuple[EvidenceKind, ...] = ()


class SubmitCall(BaseModel):
    """Submit one or more semantic evidence stages."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["submit"] = "submit"
    token: str
    evidence_kinds: tuple[EvidenceKind, ...] = ()

    @model_validator(mode="after")
    def _unique_kinds(self) -> SubmitCall:
        if len(self.evidence_kinds) != len(set(self.evidence_kinds)):
            message = "evidence kinds must be unique"
            raise ValueError(message)
        return self


class StatusCall(BaseModel):
    """Read an observable handle's durable lifecycle state."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["status"] = "status"
    token: str
    handle_id: str = Field(min_length=1)


class AwaitCall(BaseModel):
    """Wait for a handle for no longer than ``timeout_s``."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["await"] = "await"
    token: str
    handle_id: str = Field(min_length=1)
    timeout_s: FiniteFloat = Field(gt=0, le=MAX_AGENT_AWAIT_S)


class CancelCall(BaseModel):
    """Request cancellation of an owned handle."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["cancel"] = "cancel"
    token: str
    handle_id: str = Field(min_length=1)


class EvidenceCall(BaseModel):
    """Read framework-accepted evidence for the granted candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["accepted_evidence"] = "accepted_evidence"
    token: str
    evidence_kinds: tuple[EvidenceKind, ...] = ()


class RunOperationsCall(BaseModel):
    """Read recent trusted evaluation and profiler operations across the run."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["run_operations"] = "run_operations"
    token: str


MAX_STAGE_SUMMARY_TAIL_CHARS = 600


class EvaluationStageOutcome(BaseModel):
    """The trusted conclusion of one stage that recorded evidence.

    ``outcome`` is the stage's verdict: a benchmark that ran but missed its
    requirement is ``failed`` even though its evidence was recorded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: EvidenceKind
    outcome: EvidenceOutcome
    metrics: tuple[EvidenceMetric, ...] = ()
    # What a failed stage measured before it stopped, as its evaluator reported it.
    partial_measurement: PartialMeasurement | None = None
    # The end of the stage's own summary, where a failure states its cause.
    summary_tail: str | None = Field(default=None, max_length=MAX_STAGE_SUMMARY_TAIL_CHARS)


class EvaluationOperationSnapshot(BaseModel):
    """Backend-owned lifecycle and per-stage outcomes of one evaluation.

    ``evidence_recorded`` says only that every requested stage recorded
    trusted evidence; whether each stage passed is in ``stage_outcomes``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    handle_id: str = Field(min_length=1)
    state: EvaluationState
    # The stage executing now; absent before the first stage and once terminal.
    current_stage: str | None = None
    evidence_recorded: bool
    stage_outcomes: tuple[EvaluationStageOutcome, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    # Complete failure text when the evaluation failed: the run failed, or its
    # accepted evidence reports a failed outcome.
    failure: str | None = None


class EvaluationOperationObservation(BaseModel):
    """Run-wide trusted view of one role-submitted evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    handle_id: str = Field(min_length=1)
    principal_ids: tuple[str, ...]
    scope_id: str | None = None
    candidate_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_kinds: tuple[EvidenceKind, ...]
    state: EvaluationState
    # Every requested stage recorded trusted evidence; not that each passed.
    evidence_recorded: bool
    stage_outcomes: tuple[EvaluationStageOutcome, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class RunOperationsReply(BaseModel):
    """Bounded host-owned operation history available to a run observer."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["run_operations"] = "run_operations"
    evaluations: tuple[EvaluationOperationObservation, ...] = ()
    profiler_operations: tuple[ProfilerRunObservation, ...] = ()


AgentEvaluationCall = Annotated[
    AvailabilityCall
    | SubmitCall
    | StatusCall
    | AwaitCall
    | CancelCall
    | EvidenceCall
    | RunOperationsCall
    | DispatchProfilerCall
    | ProfilerOperationsCall
    | ProfilerStatusCall
    | AwaitProfilerCall
    | CancelProfilerCall,
    Field(discriminator="action"),
]


class AvailabilityReply(BaseModel):
    """Normalized availability returned to an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["availability"] = "availability"
    snapshot: AvailabilitySnapshot


class SubmittedReply(BaseModel):
    """Nonblocking submission receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["submitted"] = "submitted"
    handle_id: str


class StatusReply(BaseModel):
    """Current lifecycle state for an opaque handle."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["status"] = "status"
    handle_id: str
    status: EvaluationState


class RepeatedFailure(BaseModel):
    """A failure identical to the ones before it from the same workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    signature: str = Field(min_length=1, description="Exception type and innermost source line.")
    count: int = Field(ge=2, description="Consecutive failures with this signature, this included.")
    instruction: str = Field(min_length=1)


class EvaluationStillRunning(BaseModel):
    """A bounded await returned before the evaluation finished; it keeps running.

    Every field is the evaluation's recorded progress at return time: the
    lifecycle state, the stage executing now, and the trusted outcome of each
    stage that already finished. ``state`` is absent only when no durable read
    completed within the bound.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Literal["running"] = "running"
    handle_id: str = Field(min_length=1)
    state: EvaluationState | None
    current_stage: str | None = None
    stage_outcomes: tuple[EvaluationStageOutcome, ...] = ()
    next_await_s: FiniteFloat = Field(
        gt=0,
        le=MAX_AGENT_AWAIT_S,
        description="timeout_s for the next await_evaluation call on this handle.",
    )


AgentAwaitResult = Annotated[
    EvaluationCompleted | EvaluationStillRunning | EvaluationFailed | EvaluationCanceled,
    Field(discriminator="outcome"),
]


class AwaitReply(BaseModel):
    """Terminal result, or recorded progress when the bounded wait ended first."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["await_result"] = "await_result"
    result: AgentAwaitResult
    repeated_failure: RepeatedFailure | None = None


class CanceledReply(BaseModel):
    """State observed after requesting cancellation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["cancel_requested"] = "cancel_requested"
    handle_id: str
    status: EvaluationState


class EvidenceReply(BaseModel):
    """Trusted evidence accepted for the granted candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["accepted_evidence"] = "accepted_evidence"
    evidence: tuple[TrustedEvidence, ...]


AgentEvaluationReply = Annotated[
    AvailabilityReply
    | SubmittedReply
    | StatusReply
    | AwaitReply
    | CanceledReply
    | EvidenceReply
    | RunOperationsReply
    | ProfilerDispatchedReply
    | ProfilerOperationsReply
    | ProfilerStatusReply
    | ProfilerAwaitReply
    | ProfilerCanceledReply,
    Field(discriminator="kind"),
]


class SocketSuccess(BaseModel):
    """Successful strict socket response envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: Literal[True] = True
    result: JsonValue


class SocketFailure(BaseModel):
    """Rejected strict socket response envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: Literal[False] = False
    error: str


SocketReply = Annotated[SocketSuccess | SocketFailure, Field(discriminator="ok")]


__all__ = [
    "MAX_AGENT_AWAIT_S",
    "MAX_STAGE_SUMMARY_TAIL_CHARS",
    "AgentAwaitResult",
    "AgentEvaluationCall",
    "AgentEvaluationReply",
    "AvailabilityCall",
    "AvailabilityReply",
    "AwaitCall",
    "AwaitReply",
    "CancelCall",
    "CanceledReply",
    "EvaluationAgentRole",
    "EvaluationAgentState",
    "EvaluationGrant",
    "EvaluationOperationObservation",
    "EvaluationOperationSnapshot",
    "EvaluationStageOutcome",
    "EvaluationStillRunning",
    "EvidenceCall",
    "EvidencePreflightCheck",
    "EvidencePreflightDecision",
    "EvidencePreflightResolution",
    "EvidenceReply",
    "HandleAccess",
    "RepeatedFailure",
    "RunOperationsCall",
    "RunOperationsReply",
    "SocketFailure",
    "SocketReply",
    "SocketSuccess",
    "StatusCall",
    "StatusReply",
    "SubmitCall",
    "SubmittedReply",
    "SubmittedSemanticEvaluation",
]
