"""Exact-revision evidence, job ownership and bounded suspension values."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .common import (
    ArtifactRef,
    ContinuationId,
    Count,
    EvidenceId,
    InvocationRef,
    Observation,
    ObservationStatus,
    RequestBase,
    ResourceId,
    RevisionRef,
    Scope,
    Seconds,
    Value,
)


class SnapshotResultRef(Value):
    """Snapshot result ref lifecycle contract."""

    request_id: str = Field(min_length=1)


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


class EvidenceRef(Value):
    """Evidence ref lifecycle contract."""

    evidence_id: EvidenceId
    candidate: RevisionRef
    observation_sequence: Count
    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    provenance: Literal["trusted", "self-report"]
    status: ObservationStatus
    artifacts: tuple[ArtifactRef, ...] = ()


class OwnedJob(Value):
    """Owned job lifecycle contract."""

    resource_id: ResourceId
    scope: Scope
    plan: MeasurementPlan
    status: ObservationStatus
    terminal: bool = False
    released: bool = False
    evidence: tuple[EvidenceRef, ...] = ()


class ContinuationPhase(StrEnum):
    """Continuation phase lifecycle contract."""

    WAITING = "waiting"
    AUTHORIZED = "authorized"
    RESUMED = "resumed"
    PARKED = "parked"
    BLOCKED = "blocked"


class Continuation(Value):
    """Continuation lifecycle contract."""

    continuation_id: ContinuationId
    invocation: InvocationRef
    next_invocation: InvocationRef
    jobs: tuple[ResourceId, ...]
    deadline_at: Seconds
    phase: ContinuationPhase
    evidence: tuple[EvidenceRef, ...] = ()


class EvaluationState(Value):
    """Evaluation state lifecycle contract."""

    jobs: tuple[OwnedJob, ...] = ()
    continuations: tuple[Continuation, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()


class MeasurementRequested(Value):
    """Measurement requested lifecycle contract."""

    kind: Literal["measurement_requested"] = "measurement_requested"
    scope: Scope
    plan: MeasurementPlan


class JobObserved(Value):
    """Job observed lifecycle contract."""

    kind: Literal["job_observed"] = "job_observed"
    resource_id: ResourceId
    observation: Observation
    evidence: tuple[EvidenceRef, ...] = ()


class TurnSuspended(Value):
    """Turn suspended lifecycle contract."""

    kind: Literal["turn_suspended"] = "turn_suspended"
    continuation: Continuation


class DeadlineReached(Value):
    """Deadline reached lifecycle contract."""

    kind: Literal["deadline_reached"] = "deadline_reached"
    continuation_id: ContinuationId
    now_at: Seconds


class TimedOut(Value):
    """Timed out lifecycle contract."""

    stage: str
    queued_s: Seconds
    ran_s: Seconds
    evidence: tuple[EvidenceRef, ...] = ()


class ResumeAuthorized(Value):
    """Resume authorized lifecycle contract."""

    kind: Literal["resume_authorized"] = "resume_authorized"
    continuation_id: ContinuationId
    next_invocation: InvocationRef
    evidence: tuple[EvidenceRef, ...]
    timeout: TimedOut | None = None


class MeasurementResult(Value):
    """Measurement result lifecycle contract."""

    kind: Literal["measurement_result"] = "measurement_result"
    scope: Scope
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


type EvaluationEvent = Annotated[
    MeasurementRequested | JobObserved | TurnSuspended | DeadlineReached,
    Field(discriminator="kind"),
]
type EvaluationRequest = Annotated[
    SubmitMeasurement | ObserveOwnedJob | InspectOwnedJob | CancelOwnedJob | CollectEvidence,
    Field(discriminator="kind"),
]
