"""Strict contracts for the agent evaluation socket boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    model_validator,
)

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
    AWAIT_CAP_TEXT,
    MAX_AGENT_AWAIT_S,
    AgentToolArgs,
    AwaitProfilerCall,
    CancelProfilerCall,
    DispatchProfilerCall,
    NoArgs,
    ProfilerAwaitReply,
    ProfilerCanceledReply,
    ProfilerDispatchedReply,
    ProfilerOperationsCall,
    ProfilerOperationsReply,
    ProfilerRunObservation,
    ProfilerStatusCall,
    ProfilerStatusReply,
)

EVALUATION_ACCESS_STATE_PATH = "agent-evaluation-access.json"


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
    evaluation_suspension: bool = False


class SubmittedSemanticEvaluation(BaseModel):
    """Host-built immutable candidate identity and its durable execution handle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle_id: str = Field(min_length=1)
    fingerprints: EvidenceFingerprints


class HandleAssociation(BaseModel):
    """One requester submission and wait dependency, independent of capture ownership.

    A missing principal identifies a host capture. Inactive associations remain
    in submission history but cannot wait or authorize cancellation. The final
    live association's removal authorizes physical cancellation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: str | None = None
    generation: int = Field(ge=0)
    principal_id: str | None = Field(default=None, min_length=1)
    active: bool = True
    submission_index: int = Field(default=0, ge=0)


class EvaluationJoinExpiredError(RuntimeError):
    """The selected capture ended without reusable evidence before requester admission."""

    def __init__(self, handle_id: str, state: EvaluationState) -> None:
        """Retain the expired capture identity and authoritative terminal observation."""
        self.handle_id = handle_id
        self.state = state
        super().__init__(
            f"evaluation capture {handle_id!r} cannot accept requesters in {state.value}"
        )


class HandleAccess(BaseModel):
    """Immutable canonical capture ownership plus durable requester associations.

    ``owners`` identifies the original submitting principals and never grows
    when another requester joins. Legacy records lack associations: their
    recorded canonical scope and principals are the only reconstructible
    requester identity, at generation zero until read with the capture record.
    ``cancel_pending`` is durable cancellation intent, replayed before joining.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle_id: str = Field(min_length=1)
    scope_id: str | None = None
    fingerprints: EvidenceFingerprints
    kinds: tuple[EvidenceKind, ...]
    observers: frozenset[str]
    owners: frozenset[str]
    associations: tuple[HandleAssociation, ...] = ()
    cancel_pending: bool = False

    @model_validator(mode="after")
    def _unique_associations(self) -> HandleAccess:
        identities = [
            (item.scope_id, item.generation, item.principal_id) for item in self.associations
        ]
        if len(identities) != len(set(identities)):
            message = "evaluation requester associations must be unique"
            raise ValueError(message)
        if self.cancel_pending and any(item.active for item in self.associations):
            message = "evaluation cancellation intent requires no live requester associations"
            raise ValueError(message)
        return self

    def requesters(self, *, legacy_generation: int = 0) -> tuple[HandleAssociation, ...]:
        """Read explicit associations or the documented pre-association projection."""
        if self.associations:
            return self.associations
        return tuple(
            HandleAssociation(
                scope_id=self.scope_id, generation=legacy_generation, principal_id=principal
            )
            for principal in sorted(self.owners)
        ) or (HandleAssociation(scope_id=self.scope_id, generation=legacy_generation),)

    def associate(
        self, requester: HandleAssociation, *, capture_state: EvaluationState
    ) -> HandleAccess:
        """Purely record a submission; rejoining reactivates the exact requester."""
        if capture_state in {
            EvaluationState.FAILED,
            EvaluationState.CANCELED,
            EvaluationState.SUPERSEDED,
        }:
            raise EvaluationJoinExpiredError(self.handle_id, capture_state)
        if self.cancel_pending:
            message = f"evaluation handle {self.handle_id!r} has pending cancellation"
            raise ValueError(message)
        identity = (requester.scope_id, requester.generation, requester.principal_id)
        duplicate = next(
            (
                item
                for item in self.requesters()
                if (item.scope_id, item.generation, item.principal_id) == identity and item.active
            ),
            None,
        )
        if duplicate is not None:
            requester = requester.model_copy(
                update={"submission_index": duplicate.submission_index}
            )
        retained = tuple(
            item
            for item in self.requesters()
            if (item.scope_id, item.generation, item.principal_id) != identity
        )
        return self.model_copy(update={"associations": (*retained, requester)})

    def detach(self, *, scope_id: str | None, principal_id: str | None = None) -> HandleAccess:
        """Purely drop requester waits and persist cancellation intent when none remain."""
        associations = tuple(
            item.model_copy(update={"active": False})
            if item.scope_id == scope_id
            and (principal_id is None or item.principal_id == principal_id)
            else item
            for item in self.requesters()
        )
        return self.model_copy(
            update={
                "associations": associations,
                "cancel_pending": self.cancel_pending
                or not any(item.active for item in associations),
            }
        )


class EvaluationAgentState(BaseModel):
    """Project-owned durable access records. Bearer tokens are never persisted."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    handles: tuple[HandleAccess, ...] = ()

    def next_submission_index(self) -> int:
        """Assign requester chronology from the durable association ledger."""
        return 1 + max(
            (item.submission_index for handle in self.handles for item in handle.requesters()),
            default=0,
        )

    @model_validator(mode="after")
    def _unique_handles(self) -> EvaluationAgentState:
        ids = [item.handle_id for item in self.handles]
        if len(ids) != len(set(ids)):
            message = "evaluation access handles must be unique"
            raise ValueError(message)
        return self


def _unique_kinds(kinds: tuple[EvidenceKind, ...]) -> tuple[EvidenceKind, ...]:
    if len(kinds) != len(set(kinds)):
        message = "evidence kinds must be unique"
        raise ValueError(message)
    return kinds


EvidenceKinds = Annotated[
    tuple[EvidenceKind, ...],
    AfterValidator(_unique_kinds),
    # The validator is invisible to JSON schema; state the same rule there.
    Field(json_schema_extra={"uniqueItems": True}),
]


class EvidenceKindsArgs(AgentToolArgs):
    """Arguments naming the semantic evidence kinds a tool applies to."""

    evidence_kinds: EvidenceKinds = Field(
        default=(),
        description="Requested semantic evidence kinds. Empty means every kind granted to this role.",
    )


class HandleArgs(AgentToolArgs):
    """Arguments naming one evaluation handle."""

    handle_id: str = Field(min_length=1, description="Opaque handle returned by submit_evaluation.")


class AwaitArgs(HandleArgs):
    """Arguments of ``await_evaluation``."""

    timeout_s: FiniteFloat = Field(
        gt=0,
        description=(
            "Maximum seconds to block. Returning before completion leaves the evaluation "
            "running. " + AWAIT_CAP_TEXT
        ),
    )


class AvailabilityCall(EvidenceKindsArgs):
    """Ask for a normalized resource observation."""

    action: Literal["availability"] = "availability"
    token: str


class SubmitCall(EvidenceKindsArgs):
    """Submit one or more semantic evidence stages."""

    action: Literal["submit"] = "submit"
    token: str


class StatusCall(HandleArgs):
    """Read an observable handle's durable lifecycle state."""

    action: Literal["status"] = "status"
    token: str


class AwaitCall(AwaitArgs):
    """Wait for a handle for no longer than ``timeout_s``, capped at ``MAX_AGENT_AWAIT_S``."""

    action: Literal["await"] = "await"
    token: str


class CancelCall(HandleArgs):
    """Drop this requester's wait; cancel the capture after its last requester leaves."""

    action: Literal["cancel"] = "cancel"
    token: str


class EvidenceCall(EvidenceKindsArgs):
    """Read framework-accepted evidence for the granted candidate."""

    action: Literal["accepted_evidence"] = "accepted_evidence"
    token: str


class RunOperationsCall(NoArgs):
    """Read recent trusted evaluation and profiler operations across the run."""

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


class FailureKind(StrEnum):
    """What identifies a repeated evaluation failure."""

    # A Python traceback: its exception type and innermost source line.
    TRACEBACK = "traceback"
    # A stage that stopped early: its metric and the power-of-two range of its value.
    MEASUREMENT = "measurement"


class RepeatedFailure(BaseModel):
    """A failure of one stage identical to that stage's previous ones from the same workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: FailureKind
    stage: EvidenceKind | None = Field(
        description="The failing stage, or null when the run failed before any stage's verdict."
    )
    signature: str = Field(
        min_length=1,
        description=(
            "The kind's key fields: exception type and innermost source line, or the "
            "measured metric and its value range."
        ),
    )
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
    """Physical capture status after withdrawing this requester's association.

    Other live requesters preserve queued or running work. A terminal capture
    retains its terminal result; cancellation never rewrites accepted evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["cancel_requested"] = "cancel_requested"
    handle_id: str
    status: EvaluationState


class EvidenceReply(BaseModel):
    """Trusted evidence accepted for the granted candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["accepted_evidence"] = "accepted_evidence"
    evidence: tuple[TrustedEvidence, ...]


class RunStoppingReply(BaseModel):
    """The run is stopping, so the request started no new work.

    Returned for a new evaluation submission or profiler dispatch after a stop
    is requested. Nothing was submitted and no handle exists; end the turn.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["run_stopping"] = "run_stopping"
    instruction: str = Field(
        default=(
            "The run is stopping: no evaluation or profile was started. "
            "Do not submit more work; finish this turn now."
        ),
        min_length=1,
    )


class ScopeReleasedReply(BaseModel):
    """The orchestrator released this workspace's jobs, so the request started no new work.

    Returned for a new evaluation submission or profiler dispatch from a
    workspace scope whose queued and running jobs the orchestrator cancelled.
    Nothing was submitted and no handle exists; end the turn.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["scope_released"] = "scope_released"
    instruction: str = Field(
        default=(
            "The orchestrator released this workspace's evaluation jobs: no evaluation or "
            "profile was started, and earlier unfinished ones were cancelled. Do not submit "
            "more work; finish this turn now."
        ),
        min_length=1,
    )


class ScopeRelease(BaseModel):
    """What one release of a workspace scope's jobs requested.

    ``evaluations`` and ``profiler_operations`` are the nonterminal evaluation
    handles and profiler operations whose cancellation this release requested.
    ``first_release`` reports whether this call created the durable release
    intent. A retry of interrupted cleanup can cancel more resources while
    returning False. Once cleanup is complete, repeats return empty tuples.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str = Field(min_length=1)
    evaluations: tuple[str, ...] = ()
    profiler_operations: tuple[str, ...] = ()
    first_release: bool


class ReleasedScopesState(BaseModel):
    """Project-owned durable set of workspace scopes whose jobs are released."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    scope_ids: tuple[str, ...] = ()


AgentEvaluationReply = Annotated[
    AvailabilityReply
    | SubmittedReply
    | RunStoppingReply
    | ScopeReleasedReply
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
    "AwaitArgs",
    "AwaitCall",
    "AwaitReply",
    "CancelCall",
    "CanceledReply",
    "EvaluationAgentRole",
    "EvaluationAgentState",
    "EvaluationGrant",
    "EvaluationJoinExpiredError",
    "EvaluationOperationObservation",
    "EvaluationOperationSnapshot",
    "EvaluationStageOutcome",
    "EvaluationStillRunning",
    "EvidenceCall",
    "EvidenceKinds",
    "EvidenceKindsArgs",
    "EvidencePreflightCheck",
    "EvidencePreflightDecision",
    "EvidencePreflightResolution",
    "EvidenceReply",
    "FailureKind",
    "HandleAccess",
    "HandleArgs",
    "HandleAssociation",
    "ReleasedScopesState",
    "RepeatedFailure",
    "RunOperationsCall",
    "RunOperationsReply",
    "RunStoppingReply",
    "ScopeRelease",
    "ScopeReleasedReply",
    "SocketFailure",
    "SocketReply",
    "SocketSuccess",
    "StatusCall",
    "StatusReply",
    "SubmitCall",
    "SubmittedReply",
    "SubmittedSemanticEvaluation",
]
