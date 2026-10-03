"""Typed contracts for asynchronous profiler-agent conversations."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from vs_evaluation.agent_evidence import EvidenceKind, EvidenceOutcome, TrustedEvidence

# How long one agent await call may block before it returns progress instead.
# An agent CLI abandons an MCP tool call after its own tool-call timeout (Codex
# reported 300 s in run r15; Codex documents a 60 s default). agentshim exposes
# no provider's default, so this one bound stays under the smallest known one
# with room for the 5 s socket slack the MCP client adds.
MAX_AGENT_AWAIT_S = 45.0
MAX_PROFILER_REQUEST_CHARS = 16_384
MAX_PROFILER_NARRATIVE_CHARS = 65_536
EvidenceId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ProfilerWorkPurpose(StrEnum):
    """Framework-defined semantic purposes for delegated profiler work."""

    BOTTLENECK_ATTRIBUTION = "bottleneck_attribution"
    PLANNING_GUIDANCE = "planning_guidance"
    TARGETED_DIAGNOSTIC = "targeted_diagnostic"


class ProfilerWorkKey(BaseModel):
    """Resource-neutral identity of the question a profiler turn answers.

    ``focus`` is compared exactly.  It is semantic routing data, not a natural
    language similarity hint, so unrelated observations cannot suppress a
    framework-owned measurement.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: ProfilerWorkPurpose
    focus: str = Field(min_length=1, max_length=512)

    @field_validator("focus")
    @classmethod
    def _trim_focus(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("profiler work focus must be nonblank and trimmed")  # noqa: TRY003  # lint-waiver: LW-930051 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value


class ProfilerOperationState(StrEnum):
    """Durable lifecycle of one profiler-agent turn."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        """Return whether no further transition is possible."""
        return self in {
            self.COMPLETED,
            self.FAILED,
            self.CANCELED,
            self.INTERRUPTED,
        }


class ProfilerResultOutcome(StrEnum):
    """Typed conclusion of a successful profiler-agent turn."""

    OBSERVED = "observed"
    UNSUPPORTED = "unsupported"


class ProfilerAttribution(BaseModel):
    """Resource-neutral attribution of observed cost to one candidate component."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    cost: FiniteFloat = Field(ge=0)
    share: FiniteFloat = Field(ge=0, le=1)
    evidence: tuple[str, ...] = ()


class ProfilerAgentResult(BaseModel):
    """Advisory profiler narrative plus trusted semantic evidence references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ProfilerResultOutcome
    narrative: str = Field(max_length=MAX_PROFILER_NARRATIVE_CHARS)
    evidence_ids: tuple[EvidenceId, ...] = Field(default=(), max_length=64)
    attribution: tuple[ProfilerAttribution, ...] = Field(default=(), max_length=128)
    unsupported_reason: str | None = None

    @field_validator("evidence_ids")
    @classmethod
    def _unique_evidence(cls, value: tuple[EvidenceId, ...]) -> tuple[EvidenceId, ...]:
        if len(value) != len(set(value)):
            raise ValueError("evidence ids must be unique")  # noqa: TRY003  # lint-waiver: LW-930052 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value

    @field_validator("attribution")
    @classmethod
    def _unique_components(
        cls, value: tuple[ProfilerAttribution, ...]
    ) -> tuple[ProfilerAttribution, ...]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("profiler attribution component names must be unique")  # noqa: TRY003  # lint-waiver: LW-930053 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value

    @field_validator("unsupported_reason")
    @classmethod
    def _trim_reason(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("unsupported_reason must be nonblank and trimmed")  # noqa: TRY003  # lint-waiver: LW-930054 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value

    def model_post_init(self, __context: object) -> None:
        """Require the reason exactly when profiling is unsupported."""
        if (self.outcome is ProfilerResultOutcome.UNSUPPORTED) != (
            self.unsupported_reason is not None
        ):
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930055 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "unsupported outcome requires unsupported_reason and observed forbids it"
            )
        # An unsupported report may cite the trusted evidence it examined (for
        # example the failed capture that made the question unanswerable); the
        # host resolves those ids like any other. It may not attribute cost.
        if self.outcome is ProfilerResultOutcome.UNSUPPORTED and self.attribution:
            raise ValueError("unsupported outcome forbids attribution")  # noqa: TRY003  # lint-waiver: LW-930056 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.


class ProfilerOperationResult(BaseModel):
    """Framework-owned result joining an advisory report to validated evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: ProfilerAgentResult
    trusted_evidence: tuple[TrustedEvidence, ...] = ()

    @model_validator(mode="after")
    def _exact_profile_evidence(self) -> ProfilerOperationResult:
        if tuple(item.evidence_id for item in self.trusted_evidence) != self.report.evidence_ids:
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930057 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "trusted profile evidence references did not resolve exactly"
            )
        if any(item.kind is not EvidenceKind.PROFILE for item in self.trusted_evidence):
            raise ValueError(  # noqa: TRY003  # lint-waiver: LW-930058 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
                "trusted profile evidence references included non-profile evidence"
            )
        if self.report.outcome is ProfilerResultOutcome.OBSERVED and any(
            item.outcome is EvidenceOutcome.FAILED for item in self.trusted_evidence
        ):
            # A failed capture describes no completed workload, so it cannot
            # support an observation; the report must say unsupported instead.
            message = "an observed profile cited failed profile evidence"
            raise ValueError(message)
        return self


class ProfilerOperation(BaseModel):
    """Durable record for one requested profiler-agent turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    scope_id: str | None = None
    request: str = Field(min_length=1, max_length=MAX_PROFILER_REQUEST_CHARS)
    work: ProfilerWorkKey
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_snapshot_id: str = Field(min_length=1)
    provision_identity: str = Field(min_length=1)
    state: ProfilerOperationState
    result: ProfilerOperationResult | None = None
    error: str | None = None


class ProfilerOperationReference(BaseModel):
    """Bounded discovery record for one durable profiler-agent turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request: str = Field(min_length=1, max_length=MAX_PROFILER_REQUEST_CHARS)
    work: ProfilerWorkKey
    candidate_snapshot_id: str = Field(min_length=1)
    state: ProfilerOperationState


class ProfilerOperationLifecycle(BaseModel):
    """Bounded framework view of one principal's profiler operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request: str = Field(min_length=1, max_length=MAX_PROFILER_REQUEST_CHARS)
    work: ProfilerWorkKey
    candidate_snapshot_id: str = Field(min_length=1)
    state: ProfilerOperationState
    outcome: ProfilerResultOutcome | None = None
    trusted_evidence_ids: tuple[EvidenceId, ...] = ()


class ProfilerRunObservation(BaseModel):
    """Run-wide trusted view of one delegated profiler conversation turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    scope_id: str | None = None
    request: str = Field(min_length=1, max_length=MAX_PROFILER_REQUEST_CHARS)
    work: ProfilerWorkKey
    candidate_snapshot_id: str = Field(min_length=1)
    state: ProfilerOperationState
    evidence_recorded: bool
    outcome: ProfilerResultOutcome | None = None
    trusted_evidence_ids: tuple[EvidenceId, ...] = ()

    @model_validator(mode="after")
    def _recorded_means_trusted_evidence(self) -> ProfilerRunObservation:
        if self.evidence_recorded != bool(self.trusted_evidence_ids):
            raise ValueError("accepted profiler result must carry trusted evidence")  # noqa: TRY003  # lint-waiver: LW-092711 [TRY003]; this external contract needs a field-specific validation error; a custom exception would add a public recovery type for invalid serialized data.
        return self


class CompletedProfilerOperation(BaseModel):
    """One completed delegated result safe for framework policy to consume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    work: ProfilerWorkKey
    result: ProfilerOperationResult


class InFlightProfilerOperation(BaseModel):
    """One active delegated turn and the semantic question it answers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    work: ProfilerWorkKey


class ProfilerCandidateProjection(BaseModel):
    """Bounded framework view of delegated work for one exact candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_snapshot_id: str = Field(min_length=1)
    in_flight: tuple[InFlightProfilerOperation, ...] = ()
    completed: tuple[CompletedProfilerOperation, ...] = ()

    @model_validator(mode="after")
    def _unique_operations(self) -> ProfilerCandidateProjection:
        operation_ids = [
            *(item.operation_id for item in self.in_flight),
            *(item.operation_id for item in self.completed),
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("profiler projection operation ids must be unique")  # noqa: TRY003  # lint-waiver: LW-930059 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self


AWAIT_CAP_TEXT = (
    f"Each call waits at most {MAX_AGENT_AWAIT_S:.0f} s; a larger timeout_s waits "
    f"{MAX_AGENT_AWAIT_S:.0f} s."
)
# Nonblank, with no leading or trailing whitespace. A pattern rather than a
# validator, so the JSON schema an agent is offered states the same rule.
_TRIMMED = r"^\S(?:[\s\S]*\S)?$"


class AgentToolArgs(BaseModel):
    """The fields an agent supplies to one evaluation tool.

    Each wire call subclasses its tool's arguments and adds the host-held
    ``action`` and ``token``, so the schema an agent is offered and the model
    the service validates are one definition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class NoArgs(AgentToolArgs):
    """A tool that takes no arguments."""


class DispatchProfilerArgs(AgentToolArgs):
    """Arguments of ``dispatch_profiler``."""

    work: ProfilerWorkKey = Field(
        description=(
            "Semantic purpose and exact focus of this work. Reuse occurs only for an exact match."
        )
    )
    request: str = Field(
        min_length=1,
        max_length=MAX_PROFILER_REQUEST_CHARS,
        pattern=_TRIMMED,
        description="Natural-language profiling or measurement request.",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        description="Omit to start a conversation; provide an earlier session ID to resume it.",
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_TRIMMED,
        description="Optional retry key. Reusing it returns the original operation.",
    )


class ProfilerHandleArgs(AgentToolArgs):
    """Arguments naming one profiler operation."""

    operation_id: str = Field(
        min_length=1, description="Opaque operation ID returned by dispatch_profiler."
    )


class AwaitProfilerArgs(ProfilerHandleArgs):
    """Arguments of ``await_profiler``."""

    timeout_s: FiniteFloat = Field(
        gt=0,
        description=(
            "Maximum seconds to wait. Timeout leaves the profiler turn running. " + AWAIT_CAP_TEXT
        ),
    )


class DispatchProfilerCall(DispatchProfilerArgs):
    """Start or resume a profiler conversation without waiting for its turn."""

    action: Literal["dispatch_profiler"] = "dispatch_profiler"
    token: str


class ProfilerStatusCall(ProfilerHandleArgs):
    """Observe one profiler operation."""

    action: Literal["profiler_status"] = "profiler_status"
    token: str


class ProfilerOperationsCall(NoArgs):
    """Discover durable profiler turns owned by the logical implementer."""

    action: Literal["profiler_operations"] = "profiler_operations"
    token: str


class AwaitProfilerCall(AwaitProfilerArgs):
    """Wait at most ``timeout_s`` (capped at ``MAX_AGENT_AWAIT_S``) for one profiler operation."""

    action: Literal["await_profiler"] = "await_profiler"
    token: str


class CancelProfilerCall(ProfilerHandleArgs):
    """Request cancellation of one profiler operation."""

    action: Literal["cancel_profiler"] = "cancel_profiler"
    token: str


class ProfilerDispatchedReply(BaseModel):
    """Identifiers returned by nonblocking profiler dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["profiler_dispatched"] = "profiler_dispatched"
    session_id: str
    operation_id: str


class ProfilerStatusReply(BaseModel):
    """Current durable profiler operation record."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["profiler_status"] = "profiler_status"
    operation: ProfilerOperation


class ProfilerOperationsReply(BaseModel):
    """Recent profiler turns available to the logical implementer."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["profiler_operations"] = "profiler_operations"
    operations: tuple[ProfilerOperationReference, ...] = ()


class ProfilerAwaitReply(BaseModel):
    """Explicit completion or observational timeout from a bounded wait."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["profiler_await"] = "profiler_await"
    timed_out: bool
    operation: ProfilerOperation


class ProfilerCanceledReply(BaseModel):
    """State observed after requesting cancellation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["profiler_canceled"] = "profiler_canceled"
    operation: ProfilerOperation


class ProfilerLifecycleEvent(BaseModel):
    """Concise observation emitted for each profiler lifecycle transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str
    session_id: str
    scope_id: str | None
    request_digest: str
    state: ProfilerOperationState


__all__ = [
    "AWAIT_CAP_TEXT",
    "AgentToolArgs",
    "AwaitProfilerArgs",
    "AwaitProfilerCall",
    "CancelProfilerCall",
    "CompletedProfilerOperation",
    "DispatchProfilerArgs",
    "DispatchProfilerCall",
    "InFlightProfilerOperation",
    "NoArgs",
    "ProfilerAgentResult",
    "ProfilerAttribution",
    "ProfilerAwaitReply",
    "ProfilerCanceledReply",
    "ProfilerCandidateProjection",
    "ProfilerDispatchedReply",
    "ProfilerHandleArgs",
    "ProfilerLifecycleEvent",
    "ProfilerOperation",
    "ProfilerOperationLifecycle",
    "ProfilerOperationReference",
    "ProfilerOperationResult",
    "ProfilerOperationState",
    "ProfilerOperationsCall",
    "ProfilerOperationsReply",
    "ProfilerResultOutcome",
    "ProfilerRunObservation",
    "ProfilerStatusCall",
    "ProfilerStatusReply",
    "ProfilerWorkKey",
    "ProfilerWorkPurpose",
]
