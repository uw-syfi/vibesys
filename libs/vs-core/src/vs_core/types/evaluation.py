"""Exact-revision evidence, job ownership and bounded suspension values."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from .common import (
    ArtifactRef,
    ContinuationId,
    ContractValidationError,
    Count,
    DecisionId,
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
from .job_observations import JobProgress, JobTimeout, MeasurementFailure, TimedOut
from .until import Within
from .waits import WaitOn


class SnapshotResultRef(Value):
    """Snapshot result ref lifecycle contract."""

    request_id: RequestId


class StageRequirements(Value):
    """A stage value that may name the outputs its evidence must contain.

    ``required_outputs`` joined stages after runs had already stored digests of plans, records
    and receipts. A stage that requires nothing serializes exactly as it did before the field
    existed, so every stored digest, submission budget key and recipe digest still matches and
    a run started earlier resumes. Only a stage that requires something carries the field.
    """

    required_outputs: tuple[str, ...] = ()
    """Named outputs the stage's evidence must contain, which the executor must be able to supply.

    Names are the executor's own vocabulary; core only carries them and keys the measurement's
    identity on them, because evidence that lacks a required output cannot stand for a
    measurement that asked for it. An executor rejects a name it cannot supply.
    """

    @model_serializer(mode="wrap")
    def omit_empty_requirements(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Serialize, leaving out ``required_outputs`` when it is empty."""
        data: dict[str, object] = handler(self)
        if not data.get("required_outputs"):
            data.pop("required_outputs", None)
        return data


class MeasurementStage(StageRequirements):
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
    reusable_evidence: tuple[EvidenceId, ...] = ()
    accuracy_stage: str | None = Field(default=None, min_length=1)
    """The stage whose pass is the correctness (accuracy) gate, named explicitly.

    None declares no accuracy stage: correctness evidence then requires every
    stage to pass instead of inferring a gate from stage dependencies.
    """
    accuracy_gated: tuple[EvidenceKind, ...]
    """Evidence kinds that count only when the accuracy stage passed. Declared by the task.

    Core holds no such rule of its own: a task that wants "a benchmark counts only if
    accuracy passed" lists ``BENCHMARK`` here, a task that does not leaves it out. Naming a
    kind requires ``accuracy_stage``.
    """

    @model_validator(mode="after")
    def validate_stage_dag(self) -> MeasurementPlan:
        """Reject unknown, duplicate and cyclic requested-stage dependencies."""
        graph = {stage.stage_id: set(stage.depends_on) for stage in self.stages}
        if len(graph) != len(self.stages):
            raise MeasurementPlanError("stages", "duplicate stage ID")
        if self.accuracy_stage is not None and self.accuracy_stage not in graph:
            raise MeasurementPlanError("accuracy_stage", "unknown stage")
        if self.accuracy_gated and self.accuracy_stage is None:
            raise MeasurementPlanError("accuracy_gated", "requires an accuracy_stage")
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
        return self

    @property
    def time_bound(self) -> float:
        """The most time the declared stages and queue allowance can take.

        The plan states no deadline: how long a submission may take is the ``Within`` of the
        wait that asks for it, capped at this bound for an agent's own submission.
        """
        return self.queue_allowance + sum(stage.execution_budget for stage in self.stages)


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


_PROOF_KINDS = frozenset({EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK})


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
    def local_validation_is_never_proof(self) -> EvidenceRef:
        """Keep an agent's own evaluation out of the kinds that prove eligibility.

        Local-validation purpose records come from the agent's in-turn
        ``submit_evaluation`` tool. They are advice, never accuracy or benchmark
        proof, so they carry a kind that no eligibility requirement or
        ``accuracy_proof`` ever selects. Without this, a candidate with an agent
        record and its official record would hold two CORRECTNESS rows and
        ``accuracy_proof`` would call them ambiguous.
        """
        if self.purpose == "local-validation" and self.kind in _PROOF_KINDS:
            raise ContractValidationError(
                "kind", "a local-validation record cannot be correctness or benchmark proof"
            )
        return self

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


class ObservePacing(Value):
    """How far one job's polls have backed off.

    retries counts consecutive polls that did not move the job (unknown answers, and polls
    that ended without an answer the job took), and sets the delay before the next one. When
    the next poll goes out is the job's wait (``OwnedJob.wait``).
    """

    retries: Count = 0


def _job_wait(*, terminal: bool, wait: WaitOn | None) -> None:
    """A live job waits on its poll or its poll timer; an ended job waits on nothing."""
    if terminal != (wait is None):
        raise ContractValidationError(
            "wait", "a job waits exactly while it is not terminal (on its poll or poll timer)"
        )


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
    pacing: ObservePacing = ObservePacing()
    wait: WaitOn | None = None
    """What the live job waits on: its next poll's timer, or the poll in flight. A terminal
    job waits on nothing."""
    queue_credit: Seconds = 0
    """Seconds of queue time this job's facts earned: its limit counts from its start.

    Each interval from a fact that says the job is pending to its next fact earns its length,
    but at most ``observe_backoff_cap``: a poll that answers that often keeps the credit
    exact, and a pending fact nobody refreshes (the scheduler stopped answering) stops
    earning (``_wait_clock``)."""

    @model_validator(mode="after")
    def waits_while_live(self) -> OwnedJob:
        """Only a live job waits, and it always names what will move it."""
        _job_wait(terminal=self.terminal, wait=self.wait)
        return self


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
    pacing: ObservePacing = ObservePacing()
    wait: WaitOn | None = None
    """What the live job waits on: its operation request until that names the job, then
    its next poll's timer or the poll in flight. A terminal job waits on nothing."""
    queue_credit: Seconds = 0
    """Seconds of queue time this job's facts earned: its limit counts from its start.

    Each interval from a fact that says the job is pending to its next fact earns its length,
    but at most ``observe_backoff_cap``: a poll that answers that often keeps the credit
    exact, and a pending fact nobody refreshes (the scheduler stopped answering) stops
    earning (``_wait_clock``)."""

    @model_validator(mode="after")
    def waits_while_live(self) -> RegisteredOwnedJob:
        """Only a live job waits, and it always names what will move it."""
        _job_wait(terminal=self.terminal, wait=self.wait)
        return self


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
    spent: tuple[JobTimeout, ...] = ()
    """Jobs whose own queue budget ran out while the wait went on, in the order they ran
    out: core cancelled each, and the wait goes on with its other jobs. Each is reported
    unfinished, ``by`` ``QUEUE_BUDGET``, when the wait ends."""
    downtime_credit: Seconds = 0
    """Host downtime credited to the wait's limit while it was open, as an open request
    record's limit is moved past it (``open_host_epoch``): while no host runs nothing waits."""
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
        spent = tuple(job.resource_id for job in self.spent)
        if len(set(spent)) != len(spent) or not set(spent) <= set(self.jobs):
            raise ContractValidationError("spent", "names each of the wait's own jobs at most once")
        return self


class MeasurementStageIdentity(StageRequirements):
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
    accuracy_gated: tuple[EvidenceKind, ...]

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
                MeasurementStageIdentity(
                    stage_id=stage.stage_id,
                    depends_on=stage.depends_on,
                    required_outputs=tuple(sorted(set(stage.required_outputs))),
                )
                for stage in plan.stages
            ),
            accuracy_stage=plan.accuracy_stage,
            accuracy_gated=tuple(sorted(set(plan.accuracy_gated))),
        )

    @model_validator(mode="after")
    def canonical_stage_dag(self) -> MeasurementIdentity:
        """Canonicalize a validated dependency graph, rejecting duplicates."""
        graph = {stage.stage_id: set(stage.depends_on) for stage in self.stages}
        if len(graph) != len(self.stages):
            raise MeasurementPlanError("stages", "duplicate stage ID")
        if self.accuracy_stage is not None and self.accuracy_stage not in graph:
            raise MeasurementPlanError("accuracy_stage", "unknown stage")
        if self.accuracy_gated and self.accuracy_stage is None:
            raise MeasurementPlanError("accuracy_gated", "requires an accuracy_stage")
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
    reconciliation accounting remains RequestRecord.retry_count, not this receipt list.
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
    agent_calls: tuple[AgentCall, ...] = ()

    def evidence_for(self, key: EvidenceKey) -> EvidenceRef | None:
        """The ledger record with this full identity, or None."""
        return next((item for item in self.evidence if item.key == key), None)

    def accuracy_proof(self, candidate: RevisionRef) -> EvidenceRef | None:
        """The one accepted accuracy proof for a candidate, or None.

        Ingress admits successful CORRECTNESS evidence only after the accuracy
        stage and gate passed, so a ledger record that is trusted, successful and
        carries its acceptance receipt proves accuracy. Two such records are
        ambiguous and prove nothing. Operations that retain or promote a revision
        carry this record by value, and the owner checks it against the revision.
        """
        proofs = tuple(
            item
            for item in self.evidence
            if item.candidate == candidate
            and item.kind == EvidenceKind.CORRECTNESS
            and item.status == ObservationStatus.SUCCEEDED
            and item.provenance == "trusted"
            and item.acceptance_receipt is not None
        )
        return proofs[0] if len(proofs) == 1 else None

    def measurements_of(self, candidate: RevisionRef) -> tuple[EvidenceRef, ...]:
        """The trusted, successful official and baseline measurements of a revision.

        These are what the revision is known to measure: an agent's own local validation
        and profiling are left out. Ledger order is kept.
        """
        return tuple(
            item
            for item in self.evidence
            if item.candidate == candidate
            and item.purpose in ("official", "baseline")
            and item.status == ObservationStatus.SUCCEEDED
            and item.provenance == "trusted"
        )


class MeasurementRequested(Value):
    """Measurement requested lifecycle contract."""

    kind: Literal["measurement_requested"] = "measurement_requested"
    scope: Scope
    plan: MeasurementPlan


class AgentMeasurementRequested(Value):
    """An agent's in-turn evaluation tool call, admitted by core like a Measure decision.

    The scope is the calling principal: its owner is the attempt that holds the turn
    and its generation fences stale callers. call_id is the tool server's idempotency
    key; a replayed call never allocates a second submission. The candidate must
    already be a revision (the host snapshots the caller's workspace); budget,
    admission and identity are decided by core exactly as for Measure.
    """

    kind: Literal["agent_measurement_requested"] = "agent_measurement_requested"
    scope: Scope
    plan: MeasurementPlan
    within: Within
    """How long the submission may take: the single time limit of this request.

    Parsed from the agent's call at the boundary; core caps it at the plan's
    ``time_bound`` and at the run deadline.
    """
    call_id: str = Field(min_length=1)


class AgentRejection(StrEnum):
    """Why core refused an agent's evaluation call; nothing was charged."""

    RUN_STOPPING = "run_stopping"
    """The run is closing or has ended, so it starts no new work (a paused run still does)."""
    NOT_ADMITTED = "not_admitted"
    """The scope is not a current admitted owner: a stale generation, or a closed attempt."""
    INVALID_PLAN = "invalid_plan"
    """The plan names no candidate revision, so no measurement identity exists."""
    NOT_ALLOWED = "not_allowed"
    """The identity's submission budget is spent, or the plan exceeds a run limit or deadline."""


class AgentCall(Value):
    """Record of one admitted or rejected agent tool call, keyed by its call_id.

    A call that asks for a candidate its scope already measures (same identity) is a
    ``repeat``: it names the existing submission and charged nothing. Every other admitted
    call allocated the submission it names. ``deadline_at`` is the submission's deadline.
    """

    call_id: str
    scope: Scope
    request_id: RequestId | None = None
    """The submission it names, or None when core rejected the call."""
    deadline_at: Seconds | None = None
    """The named submission's stated deadline, bounded by the run's; None when rejected.

    Answered as the call time plus the limit, and never moved: it is what the agent was told.
    A wait on the job reads the limit from the submission's record, which a host restart
    moves past its downtime, and adds the job's queue credit (``_wait_clock``)."""
    repeat: bool = False
    """Whether the call named a submission an earlier call (or a Measure) had allocated."""
    rejection: AgentRejection | None = None

    @classmethod
    def refused(
        cls, event: AgentMeasurementRequested, rejection: AgentRejection | None
    ) -> AgentCall:
        """The record of a call core refused; nothing was charged."""
        if rejection is None:
            message = "a refused call names its rejection"
            raise ContractValidationError("rejection", message)
        return cls(call_id=event.call_id, scope=event.scope, rejection=rejection)

    @classmethod
    def answered(
        cls,
        event: AgentMeasurementRequested,
        request_id: RequestId,
        deadline_at: Seconds,
        *,
        repeat: bool,
    ) -> AgentCall:
        """The record of a call core answered with a submission."""
        return cls(
            call_id=event.call_id,
            scope=event.scope,
            request_id=request_id,
            deadline_at=deadline_at,
            repeat=repeat,
        )

    @model_validator(mode="after")
    def names_a_submission_or_a_rejection(self) -> AgentCall:
        """Exactly one of a rejection and a submission with its deadline."""
        answered = self.request_id is not None
        if answered == (self.rejection is not None):
            raise ContractValidationError("rejection", "a call has a request or a rejection")
        if answered != (self.deadline_at is not None) or (self.repeat and not answered):
            raise ContractValidationError("deadline_at", "only an answered call has a deadline")
        return self


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
    # The executor's classification of a failed job. It narrows what the observation
    # proves (see `MeasurementFailure`) and lets the submission be retried only when
    # it says infrastructure.
    failure: MeasurementFailure | None = None
    operator_diagnostic: str = ""
    """Credential-safe host projection kept separate from the raw diagnostic."""

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
    operator_diagnostic: str = ""
    """Credential-safe host projection kept separate from the raw diagnostic."""

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


class JobWaitLapsed(Value):
    """A request a live job waits on ended without moving the job.

    Internal signal raised once a step's other signals settle, never shell input: the poll
    blocked at its bound, or the job refused its answer (stale or foreign). The job polls
    again after its backoff instead of waiting on a request that is over.
    """

    kind: Literal["job_wait_lapsed"] = "job_wait_lapsed"
    job: RequestId
    """The job's own request: a measurement's submission, or a registered operation."""
    request_id: RequestId


class ObservationsDue(Value):
    """Core time reached now_at: poll every job whose wait timer has come.

    Internal signal raised whenever the clock advances, never shell input.
    """

    kind: Literal["observations_due"] = "observations_due"
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


MEASUREMENT_DIAGNOSTIC_LIMIT = 4000


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
    diagnostic: str = ""
    """The executor's raw failure text, bounded to its trailing
    ``MEASUREMENT_DIAGNOSTIC_LIMIT`` characters.

    It is the only account of a failure that left no evidence, such as a benchmark that
    died without a result record, so the strategy can tell the agent why it was given up.
    """
    operator_diagnostic: str = ""
    """Credential-safe bounded projection for operator-facing events only."""


class JobQueued(Value):
    """A job still waits in its executor's queue: core reports it each ``queue_notice_interval``.

    Published for operators; a search does not receive it. ``pending_reason`` is the
    executor's own reason code (Slurm's ``Priority``, ``Resources``, ``ReqNodeNotAvail``),
    or None when the executor gave none: core never invents one.
    """

    kind: Literal["job_queued"] = "job_queued"
    scope: Scope
    resource_id: ResourceId
    source_request: RequestId
    """The request that started the job: its submission, or its registered operation."""
    queued_s: Seconds
    """Queue time the job has been credited so far."""
    budget_left_s: Seconds
    """Queue time left before core ends the measurement with ``QUEUE_BUDGET_SPENT``."""
    pending_reason: str | None = None


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


class RunJobsCancelRequested(Value):
    """A committed cancel stop ends the jobs its scope owns that no attempt closure drains.

    ``authority`` is the stop's decision; evaluation acts only while it is the run's first
    committed stop, in cancel mode, for this scope.
    """

    kind: Literal["run_jobs_cancel_requested"] = "run_jobs_cancel_requested"
    scope: Scope
    authority: DecisionId


class MeasurementSubmissionObserved(Value):
    """Classified submission observation updates its immutable budget receipt."""

    kind: Literal["measurement_submission_observed"] = "measurement_submission_observed"
    observation: Observation
    failure: MeasurementFailure | None = None


type EvaluationEvent = Annotated[
    RegisteredJobObserved
    | RegisteredJobRequested
    | MeasurementRequested
    | AgentMeasurementRequested
    | JobObserved
    | TurnSuspended
    | DeadlineReached
    | ObservationsDue
    | JobWaitLapsed
    | ContinuationJobsChanged
    | JobTerminationRequested
    | ContinuationRetireRequested
    | ContinuationReopenRequested
    | ContinuationScopeReopened
    | JobsDrainRequested
    | RunJobsCancelRequested
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
