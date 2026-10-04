"""Validated observations for the cluster job interface."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

from .runner import (
    SlurmBatchHandle,
    SlurmBatchResult,
    SlurmJobHandle,
    SlurmJobResult,
    SlurmJobStatus,
)

ClusterHandle: TypeAlias = SlurmJobHandle | SlurmBatchHandle
ClusterResult: TypeAlias = SlurmJobResult | SlurmBatchResult
ClusterTarget: TypeAlias = str | ClusterHandle


class _Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class ClusterSubmitted(_Outcome):
    """Scheduler acceptance with its durable locator."""

    kind: Literal["submitted"] = "submitted"
    operation_id: str
    handle: ClusterHandle


class ClusterConflict(_Outcome):
    """Stable identity reused with a different payload."""

    kind: Literal["conflict"] = "conflict"
    operation_id: str
    reason: str


class ClusterRejected(_Outcome):
    """Submission known not to have reached the scheduler."""

    kind: Literal["rejected"] = "rejected"
    operation_id: str
    reason: str


class ClusterUnknown(_Outcome):
    """Ambiguous observation with identity and preserved evidence."""

    kind: Literal["unknown"] = "unknown"
    operation_id: str | None
    reason: str
    job_id: str | None = None
    result: ClusterResult | None = None


class ClusterObservation(_Outcome):
    """Scheduler observation without lifecycle policy."""

    kind: Literal["observed"] = "observed"
    operation_id: str | None
    job_id: str
    status: SlurmJobStatus
    pending_reason: str | None = None
    estimated_start: str | None = None
    handle: ClusterHandle | None = None


class ClusterCancelRequested(_Outcome):
    """Cancellation intent acknowledged; inspect confirms termination separately."""

    kind: Literal["cancel_requested"] = "cancel_requested"
    operation_id: str | None
    job_id: str | None = None


class ClusterCollected(_Outcome):
    """Complete terminal evidence for one operation."""

    kind: Literal["collected"] = "collected"
    operation_id: str | None
    result: ClusterResult


ClusterSubmitOutcome: TypeAlias = (
    ClusterSubmitted | ClusterConflict | ClusterRejected | ClusterUnknown
)
ClusterInspectOutcome: TypeAlias = ClusterObservation | ClusterUnknown
ClusterCancelOutcome: TypeAlias = ClusterCancelRequested | ClusterUnknown
ClusterCollectOutcome: TypeAlias = ClusterCollected | ClusterUnknown
