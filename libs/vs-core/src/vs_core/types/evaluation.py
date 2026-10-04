"""Exact-revision evidence, job ownership and bounded suspension values."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ContinuationId,
    ContractValidationError,
    Count,
    EvidenceId,
    EvidenceKey,
    EvidenceKind,
    ExecuteRegisteredOperation,
    InvocationRef,
    Observation,
    ObservationStatus,
    OperationId,
    PoolId,
    RequestBase,
    RequestId,
    ResourceId,
    RevisionRef,
    Scope,
    ScopeReopenNormalization,
    Seconds,
    Value,
)
from .evaluation_history import (
    EvaluationHistoryCursor,
    EvaluationTerminalFacts,
    RepeatedFailureGuidance,
)
from .job_observations import JobProgress, MeasurementFailure, TimedOut


class SnapshotResultRef(Value):
    """Snapshot result ref lifecycle contract."""

    request_id: RequestId


class MeasurementStage(Value):
    """Measurement stage lifecycle contract."""

    stage_id: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    execution_budget: Seconds


class MeasurementPlan(Value):
    """Measurement plan lifecycle contract."""

    purpose: Literal["baseline", "local-validation", "official", "profile"]
    candidate: RevisionRef | SnapshotResultRef
    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    stages: tuple[MeasurementStage, ...]
    policy: Literal["ordered", "parallel"]
    recipe: ArtifactRef
    submitted_at: Seconds
    queue_allowance: Seconds
    deadline_at: Seconds
    reusable_evidence: tuple[EvidenceId, ...] = ()
    submission_limit: int = Field(default=1, ge=1)
    accuracy_stage: str | None = Field(default=None, min_length=1)
    """The stage whose pass is the correctness (accuracy) gate, named explicitly.

    None declares no accuracy stage: correctness evidence then requires every
    stage to pass instead of inferring a gate from stage dependencies.
    """

    @model_validator(mode="after")
    def validate_stage_dag(self) -> MeasurementPlan:
        """Reject unknown, duplicate and cyclic requested-stage dependencies."""
        graph = {stage.stage_id: set(stage.depends_on) for stage in self.stages}
        if len(graph) != len(self.stages):
            raise MeasurementPlanError("stages", "duplicate stage ID")
        if self.accuracy_stage is not None and self.accuracy_stage not in graph:
            raise MeasurementPlanError("accuracy_stage", "unknown stage")
        for stage in self.stages:
            if set(stage.depends_on) - graph.keys():
                raise MeasurementPlanError(stage.stage_id, "unknown dependency")
        remaining = graph.copy()
        while remaining:
            ready = {name for name, dependencies in remaining.items() if not dependencies}
            if not ready:
                raise MeasurementPlanError("stages", "cyclic dependency")
            remaining = {
                name: dependencies - ready
                for name, dependencies in remaining.items()
                if name not in ready
            }
        maximum = (
            self.submitted_at
            + self.queue_allowance
            + sum(stage.execution_budget for stage in self.stages)
        )
        if not self.submitted_at <= self.deadline_at <= maximum:
            raise MeasurementPlanError("deadline_at", "deadline exceeds declared stage bound")
        return self


class EvidenceAcceptanceReceipt(Value):
    """Frozen observation accepted when its exact evidence entered the ledger.

    Evaluation A validates source, scope, sequence and measurement identity at
    insertion. Later job observations never replace this original receipt.
    Historical evidence without a receipt grants no independently verifiable
    acceptance authority to settlement.
    """

    observation: Observation

    @model_validator(mode="after")
    def positive_acceptance(self) -> EvidenceAcceptanceReceipt:
        """Unknown or rejected acceptance cannot certify evidence ingestion."""
        if not self.observation.accepted or self.observation.status == ObservationStatus.UNKNOWN:
            raise ContractValidationError("observation", "positive source acceptance required")
        return self


class EvidenceRef(Value):
    """Evidence ref lifecycle contract."""

    evidence_id: EvidenceId
    kind: EvidenceKind
    purpose: Literal["baseline", "local-validation", "official", "profile"]
    scope: Scope
    source_request: RequestId
    candidate: RevisionRef
    observation_sequence: Count
    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    provenance: Literal["trusted", "self-report"]
    status: ObservationStatus
    artifacts: tuple[ArtifactRef, ...] = ()
    acceptance_receipt: EvidenceAcceptanceReceipt | None = None

    @property
    def key(self) -> EvidenceKey:
        """Run-wide identity: evidence IDs are unique only within a source request."""
        return EvidenceKey(source_request=self.source_request, evidence_id=self.evidence_id)

    @model_validator(mode="after")
    def original_acceptance(self) -> EvidenceRef:
        """Bind historical source proof without inferring absent authority."""
        if self.acceptance_receipt is not None:
            observation = self.acceptance_receipt.observation
            if (
                observation.request_id != self.source_request
                or observation.scope != self.scope
                or observation.sequence != self.observation_sequence
                or observation.status != self.status
            ):
                raise ContractValidationError(
                    "acceptance_receipt", "source, scope, sequence and status must match evidence"
                )
        return self


class OwnedJob(Value):
    """Built-in job ownership tied to its canonical submission request.

    observation and progress retain original sequence/time. Validate measurement
    stage IDs against the plan registry and preserve late discovered descendants.
    Terminal execution and positive release are separate required cleanup facts.
    """

    resource_id: ResourceId
    submission_id: RequestId
    scope: Scope
    observation: Observation | None = None
    progress: JobProgress | None = None
    children: tuple[ResourceId, ...] = ()
    plan: MeasurementPlan
    status: ObservationStatus
    terminal: bool = False
    released: bool = False
    evidence: tuple[EvidenceRef, ...] = ()


class RegisteredOwnedJob(Value):
    """Generic job ownership independent of built-in measurement plans.

    Progress uses the registered owner library's validated stage contract. Late
    identity and children join ownership before provisional request edges clear.
    No synthetic measurement plan is constructed for discovered generic jobs.
    expected_measurement is the registered canonical payload normalization,
    never inferred from submitted evidence. None grants no measurement eligibility.
    """

    operation_id: OperationId
    expected_measurement: MeasurementIdentity | None = None
    observation: Observation | None = None
    progress: JobProgress | None = None
    request_id: RequestId
    scope: Scope
    resource_pool: PoolId
    resource_id: ResourceId | None = None
    status: ObservationStatus = ObservationStatus.PENDING
    terminal: bool = False
    released: bool = False
    children: tuple[ResourceId, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()


class ContinuationPhase(StrEnum):
    """Continuation phase lifecycle contract."""

    WAITING = "waiting"
    AUTHORIZED = "authorized"
    RESUMED = "resumed"
    PARKED = "parked"
    CANCELLED = "cancelled"
    REOPENING = "reopening"
    BLOCKED = "blocked"


class ResumeAuthorizationReceipt(Value):
    """Immutable publication proof, independent of the current continuation phase.

    Continuations B stores this atomically with the single ResumeAuthorized event.
    Parking/reopening retains the same receipt and logical successor. Only a
    confirmed successor dispatch moves it to RESUMED, never republishes feedback.
    A None history cursor means unavailable, never a certified empty history.
    """

    continuation_id: ContinuationId
    next_invocation: InvocationRef
    evidence: tuple[EvidenceRef, ...]
    history_cursor: EvaluationHistoryCursor | None
    timeout: TimedOut | None = None
    repeated_failure: RepeatedFailureGuidance | None = None

    @model_validator(mode="after")
    def guidance_cursor(self) -> ResumeAuthorizationReceipt:
        """Repeated failure facts name this exact publication history prefix."""
        if (
            self.repeated_failure is not None
            and self.repeated_failure.cursor != self.history_cursor
        ):
            raise ContractValidationError(
                "repeated_failure.cursor", "differs from publication cursor"
            )
        return self


class Continuation(Value):
    """One wait-all authorization with frozen timeout and exact park ownership.

    Cancellation is terminal. Reopening requires exact cancelled-job resolutions,
    current park authority, FIFO capacity and positive retained lease reacquisition.
    Only confirmed external reopen permits ResumeAuthorized; actual resume uses
    next_invocation. Unknown reopen retains the new slot and ownership fences.
    preceding_submission is the exact previous ResumeAuthorized publication
    cursor, distinct from the original paid-cycle prefix; None means absent or
    unavailable prior publication authority.
    """

    continuation_id: ContinuationId
    invocation: InvocationRef
    next_invocation: InvocationRef
    jobs: tuple[ResourceId, ...]
    deadline_at: Seconds
    phase: ContinuationPhase
    timeout: TimedOut | None = None
    park_authority: RequestId | None = None
    reopen_authority: RequestId | None = None
    cancelled_resolutions: tuple[ResourceId, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    preceding_submission: EvaluationHistoryCursor | None = None
    authorization_receipt: ResumeAuthorizationReceipt | None = None

    @model_validator(mode="after")
    def publication_identity(self) -> Continuation:
        """Require any independent publication receipt to name this successor."""
        receipt = self.authorization_receipt
        if receipt is not None and (
            receipt.continuation_id != self.continuation_id
            or receipt.next_invocation != self.next_invocation
        ):
            raise ContractValidationError(
                "authorization_receipt", "continuation/successor mismatch"
            )
        if receipt is not None and receipt.timeout != self.timeout:
            raise ContractValidationError(
                "authorization_receipt.timeout", "differs from frozen timeout"
            )
        return self


class MeasurementStageIdentity(Value):
    """Stage membership and dependencies, independent of execution budget."""

    stage_id: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()


class MeasurementIdentity(Value):
    """Submission budget key after snapshot resolution.

    Identity excludes times, deadlines, allowances, budgets, scheduling policy
    and reuse hints, so changing these never resets submission allowance.
    Stages and dependency order are canonicalized; revision ID and digest both
    participate alongside the recipe and all three execution fingerprints.
    """

    purpose: Literal["baseline", "local-validation", "official", "profile"]
    candidate: RevisionRef
    evaluator_digest: str = Field(min_length=1)
    workload_digest: str = Field(min_length=1)
    environment_digest: str = Field(min_length=1)
    recipe_digest: str = Field(min_length=1)
    stages: tuple[MeasurementStageIdentity, ...]
    accuracy_stage: str | None = Field(default=None, min_length=1)

    @classmethod
    def from_plan(cls, plan: MeasurementPlan, candidate: RevisionRef) -> MeasurementIdentity:
        """The one projection from a plan and its resolved revision to its identity."""
        return cls(
            purpose=plan.purpose,
            candidate=candidate,
            evaluator_digest=plan.evaluator_digest,
            workload_digest=plan.workload_digest,
            environment_digest=plan.environment_digest,
            recipe_digest=plan.recipe.digest,
            stages=tuple(
                MeasurementStageIdentity(stage_id=stage.stage_id, depends_on=stage.depends_on)
                for stage in plan.stages
            ),
            accuracy_stage=plan.accuracy_stage,
        )

    @model_validator(mode="after")
    def canonical_stage_dag(self) -> MeasurementIdentity:
        """Canonicalize a validated dependency graph, rejecting duplicates."""
        graph = {stage.stage_id: set(stage.depends_on) for stage in self.stages}
        if len(graph) != len(self.stages):
            raise MeasurementPlanError("stages", "duplicate stage ID")
        if self.accuracy_stage is not None and self.accuracy_stage not in graph:
            raise MeasurementPlanError("accuracy_stage", "unknown stage")
        for stage in self.stages:
            if len(set(stage.depends_on)) != len(stage.depends_on):
                raise MeasurementPlanError(stage.stage_id, "duplicate dependency")
            if set(stage.depends_on) - graph.keys():
                raise MeasurementPlanError(stage.stage_id, "unknown dependency")
        remaining = graph.copy()
        while remaining:
            ready = {name for name, dependencies in remaining.items() if not dependencies}
            if not ready:
                raise MeasurementPlanError("stages", "cyclic dependency")
            remaining = {
                name: dependencies - ready
                for name, dependencies in remaining.items()
                if name not in ready
            }
        canonical = tuple(
            stage.model_copy(update={"depends_on": tuple(sorted(stage.depends_on))})
            for stage in sorted(self.stages, key=lambda stage: stage.stage_id)
        )
        object.__setattr__(self, "stages", canonical)
        return self


class PreparedSubmissionReceipt(Value):
    """One stable submission ordinal allocated atomically with its request.

    Preacceptance failure consumes the ordinal. Replay of the same request does
    not. Unknown acceptance requires reconciliation before another submission.
    """

    kind: Literal["prepared"] = "prepared"
    request_id: RequestId
    ordinal: int = Field(ge=1)
    observation: Observation | None = None
    failure: MeasurementFailure | None = None


class HistoricalSubmissionReceipt(Value):
    """Migration budget consumption, granting no request or lifecycle authority."""

    kind: Literal["historical"] = "historical"
    ordinal: int = Field(ge=1)
    proof: ArtifactRef


type SubmissionReceipt = Annotated[
    PreparedSubmissionReceipt | HistoricalSubmissionReceipt, Field(discriminator="kind")
]


class SubmissionBudget(Value):
    """Immutable first submission bound for one scope and measurement identity.

    The first limit cannot exceed the run ceiling. Workload rejection is durable;
    conclusive infrastructure failure may retry within this bound. Transport
    reconciliation accounting remains Intent.retry_count, not this receipt list.
    """

    scope: Scope
    identity: MeasurementIdentity
    limit: int = Field(ge=1)
    receipts: tuple[SubmissionReceipt, ...] = ()

    @model_validator(mode="after")
    def contiguous_receipts(self) -> SubmissionBudget:
        """Require unique contiguous ordinals and stable distinct request IDs."""
        if tuple(receipt.ordinal for receipt in self.receipts) != tuple(
            range(1, len(self.receipts) + 1)
        ):
            raise ContractValidationError(
                "receipts", "ordinals must be unique and contiguous from 1"
            )
        if len(self.receipts) > self.limit:
            raise ContractValidationError("receipts", "exceeds submission limit")
        ids = tuple(
            receipt.request_id
            for receipt in self.receipts
            if isinstance(receipt, PreparedSubmissionReceipt)
        )
        if len(set(ids)) != len(ids):
            raise ContractValidationError("receipts", "duplicate request ID")
        return self


class EvaluationState(Value):
    """Evaluation state lifecycle contract."""

    jobs: tuple[OwnedJob, ...] = ()
    registered_jobs: tuple[RegisteredOwnedJob, ...] = ()
    continuations: tuple[Continuation, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    submission_budgets: tuple[SubmissionBudget, ...] = ()


class MeasurementRequested(Value):
    """Measurement requested lifecycle contract."""

    kind: Literal["measurement_requested"] = "measurement_requested"
    scope: Scope
    plan: MeasurementPlan


class RegisteredJobRequested(Value):
    """Custom jobs acquire their declared pool through evaluation ownership."""

    kind: Literal["registered_job_requested"] = "registered_job_requested"
    request: ExecuteRegisteredOperation
    resource_pool: PoolId
    expected_measurement: MeasurementIdentity | None = None


class JobObserved(Value):
    """Job observed lifecycle contract."""

    kind: Literal["job_observed"] = "job_observed"
    progress: JobProgress | None = None
    resource_id: ResourceId
    observation: Observation
    evidence: tuple[EvidenceRef, ...] = ()
    evaluation_result: EvaluationTerminalFacts | None = None

    @model_validator(mode="after")
    def correlated_progress(self) -> JobObserved:
        """Keep progress sequence and time equal to the carrying observation."""
        if self.progress is not None and (
            self.progress.observation_sequence != self.observation.sequence
            or self.progress.observed_at != self.observation.observed_at
        ):
            raise ContractValidationError("progress", "sequence/time differs from observation")
        if self.evaluation_result is not None:
            self.evaluation_result.validate_observation(self.observation)
        return self


class RegisteredJobObserved(Value):
    """Late generic resource identities join the owning job ledger."""

    kind: Literal["registered_job_observed"] = "registered_job_observed"
    progress: JobProgress | None = None
    operation_id: OperationId
    observation: Observation
    evidence: tuple[EvidenceRef, ...] = ()
    evaluation_result: EvaluationTerminalFacts | None = None

    @model_validator(mode="after")
    def correlated_progress(self) -> RegisteredJobObserved:
        """Keep progress sequence and time equal to the carrying observation."""
        if self.progress is not None and (
            self.progress.observation_sequence != self.observation.sequence
            or self.progress.observed_at != self.observation.observed_at
        ):
            raise ContractValidationError("progress", "sequence/time differs from observation")
        if self.evaluation_result is not None:
            self.evaluation_result.validate_observation(self.observation)
        return self


class TurnSuspended(Value):
    """Turn suspended lifecycle contract."""

    kind: Literal["turn_suspended"] = "turn_suspended"
    continuation: Continuation


class DeadlineReached(Value):
    """Deadline reached lifecycle contract."""

    kind: Literal["deadline_reached"] = "deadline_reached"
    continuation_id: ContinuationId
    now_at: Seconds


class ResumeAuthorized(Value):
    """One strategy feedback authorization for the canonical next invocation.

    timeout is the continuation's stored frozen value unchanged. Late results
    can close cleanup obligations but cannot rewrite this feedback. Strategy
    separately renders and proposes RequestTurn; feedback itself dispatches none.
    A None history cursor preserves unavailable history without inventing zero.
    """

    kind: Literal["resume_authorized"] = "resume_authorized"
    continuation_id: ContinuationId
    next_invocation: InvocationRef
    evidence: tuple[EvidenceRef, ...]
    timeout: TimedOut | None = None
    history_cursor: EvaluationHistoryCursor | None = None
    repeated_failure: RepeatedFailureGuidance | None = None

    @model_validator(mode="after")
    def guidance_cursor(self) -> ResumeAuthorized:
        """Strategy feedback cannot name another failure history prefix."""
        if (
            self.repeated_failure is not None
            and self.repeated_failure.cursor != self.history_cursor
        ):
            raise ContractValidationError(
                "repeated_failure.cursor", "differs from publication cursor"
            )
        return self


class MeasurementResult(Value):
    """Measurement result retains evidence and typed submission failure.

    Preserve partial measurements and diagnostics in owning-library artifacts.
    Unknown classification is never inferred to be a workload rejection, and
    successful external execution is not itself a successful correctness gate.
    """

    kind: Literal["measurement_result"] = "measurement_result"
    failure: MeasurementFailure | None = None
    scope: Scope
    source_request: RequestId | None = None
    """The submission request this result reports, keying it with its scope.

    None only when no request was prepared: rejection at admission, or a result
    served entirely from reusable evidence.
    """
    evidence: tuple[EvidenceRef, ...]
    status: ObservationStatus


class SubmitMeasurement(RequestBase):
    """Submit measurement lifecycle contract."""

    kind: Literal["submit_measurement"] = "submit_measurement"
    plan: MeasurementPlan


class ObserveOwnedJob(RequestBase):
    """Observe owned job lifecycle contract."""

    kind: Literal["observe_owned_job"] = "observe_owned_job"
    resource_id: ResourceId


class InspectOwnedJob(RequestBase):
    """Inspect owned job lifecycle contract."""

    kind: Literal["inspect_owned_job"] = "inspect_owned_job"
    resource_id: ResourceId


class CancelOwnedJob(RequestBase):
    """Cancel owned job lifecycle contract."""

    kind: Literal["cancel_owned_job"] = "cancel_owned_job"
    resource_id: ResourceId


class CollectEvidence(RequestBase):
    """Collect evidence lifecycle contract."""

    kind: Literal["collect_evidence"] = "collect_evidence"
    resource_id: ResourceId


class UnobservedJobFacts(Value):
    """Positive prior-state snapshot that no job observation had been accepted."""

    kind: Literal["unobserved"] = "unobserved"
    resource_id: ResourceId


class ObservedJobFacts(Value):
    """Immutable pre-update owner facts used to freeze exact/late deadlines.

    Measurements A snapshots these before accepting the carrying new observation.
    Continuations B substitutes these facts for this resource when freezing the
    deadline, while reading unchanged other dependencies from EvaluationContext.
    Retained progress may precede the latest observation and keeps its original
    sequence/time. Its issuer validates accepted provenance; sequences from
    different request sources are not comparable. Missing progress stays missing;
    terminal and release remain independent.
    """

    kind: Literal["observed"] = "observed"
    resource_id: ResourceId
    observation: Observation
    progress: JobProgress | None = None
    evidence: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def prior_correspondence(self) -> ObservedJobFacts:
        """Correlate resource while preserving the older accepted progress fact."""
        if (
            self.observation.resource_id is not None
            and self.observation.resource_id != self.resource_id
        ):
            raise ContractValidationError("observation", "resource ID mismatch")
        if self.progress is not None and self.progress.observed_at > self.observation.observed_at:
            raise ContractValidationError("progress", "time follows latest prior observation")
        return self


type JobFactsBeforeObservation = Annotated[
    UnobservedJobFacts | ObservedJobFacts, Field(discriminator="kind")
]


class ContinuationJobsChanged(Value):
    """New job facts plus required pre-update receipt wake wait-all processing.

    Freeze any reached deadline using previous before inspecting the newly
    accepted fact. Measurements A is the sole issuer and preserves this snapshot
    at its atomic update boundary. No default can fabricate prior-state proof.
    """

    kind: Literal["continuation_jobs_changed"] = "continuation_jobs_changed"
    resource_id: ResourceId
    observation: Observation
    previous: JobFactsBeforeObservation

    @model_validator(mode="after")
    def exact_resource(self) -> ContinuationJobsChanged:
        """Require the prior-state receipt to name the changed resource."""
        if self.previous.resource_id != self.resource_id:
            raise ContractValidationError("previous", "resource ID mismatch")
        if self.observation.resource_id != self.resource_id:
            raise ContractValidationError("observation", "resource ID mismatch")
        if isinstance(self.previous, ObservedJobFacts):
            prior = self.previous.observation
            if prior.scope != self.observation.scope:
                raise ContractValidationError("previous", "scope differs from carrying observation")
            if prior.observed_at > self.observation.observed_at:
                raise ContractValidationError("previous", "time follows carrying observation")
            if (
                prior.request_id == self.observation.request_id
                and prior.sequence >= self.observation.sequence
            ):
                raise ContractValidationError(
                    "previous", "sequence does not precede carrying observation"
                )
        return self

    @property
    def observation_sequence(self) -> Count:
        """Project sequence from the sole carrying observation."""
        return self.observation.sequence

    @property
    def observed_at(self) -> Seconds:
        """Project time from the sole carrying observation."""
        return self.observation.observed_at


class JobTerminationRequested(Value):
    """Timeout or retirement requests termination, never infers it."""

    kind: Literal["job_termination_requested"] = "job_termination_requested"
    resource_id: ResourceId
    cause: Literal["deadline", "retirement"]


class ContinuationRetireRequested(Value):
    """Park or cancel continuation using exact canonical scope-close authority."""

    kind: Literal["continuation_retire_requested"] = "continuation_retire_requested"
    continuation_id: ContinuationId
    disposition: Literal["park", "cancel"]
    park_authority: RequestId | None = None


class ContinuationReopenRequested(Value):
    """Kernel routes normalized guarded reopening to evaluation authority."""

    kind: Literal["continuation_reopen_requested"] = "continuation_reopen_requested"
    request: ExecuteRegisteredOperation
    normalization: ScopeReopenNormalization


class ContinuationScopeReopened(Value):
    """Positive exact park-authority proof permits canonical resume authorization."""

    kind: Literal["continuation_scope_reopened"] = "continuation_scope_reopened"
    continuation_id: ContinuationId
    park_authority: RequestId
    observation: Observation


class JobsDrainRequested(Value):
    """Drain owned jobs and captures before closing evaluation endpoint."""

    kind: Literal["jobs_drain_requested"] = "jobs_drain_requested"
    scope: Scope
    authority: RequestId
    disposition: Literal["park", "cancel", "settle"]


class MeasurementSubmissionObserved(Value):
    """Classified submission observation updates its immutable budget receipt."""

    kind: Literal["measurement_submission_observed"] = "measurement_submission_observed"
    observation: Observation
    failure: MeasurementFailure | None = None


type EvaluationEvent = Annotated[
    RegisteredJobObserved
    | RegisteredJobRequested
    | MeasurementRequested
    | JobObserved
    | TurnSuspended
    | DeadlineReached
    | ContinuationJobsChanged
    | JobTerminationRequested
    | ContinuationRetireRequested
    | ContinuationReopenRequested
    | ContinuationScopeReopened
    | JobsDrainRequested
    | MeasurementSubmissionObserved,
    Field(discriminator="kind"),
]
type EvaluationRequest = Annotated[
    SubmitMeasurement | ObserveOwnedJob | InspectOwnedJob | CancelOwnedJob | CollectEvidence,
    Field(discriminator="kind"),
]


class MeasurementPlanError(ValueError):
    """Invalid evidence dependencies or absolute deadline at ingress."""

    def __init__(self, path: str, detail: str) -> None:
        """Name the invalid stage or deadline field."""
        super().__init__(f"{path}: {detail}")
