"""Validated observations for the cluster job interface."""

from __future__ import annotations

import re
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, model_validator

from .runner import (
    SlurmBatchHandle,
    SlurmBatchResult,
    SlurmError,
    SlurmJobHandle,
    SlurmJobResult,
    SlurmJobStatus,
)

ClusterHandle: TypeAlias = SlurmJobHandle | SlurmBatchHandle
ClusterResult: TypeAlias = SlurmJobResult | SlurmBatchResult
ClusterTarget: TypeAlias = str | ClusterHandle


def validate_operation_id(value: str) -> None:
    """Require a safe stable identifier before recording or executing I/O."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise SlurmError.invalid_operation_id()


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
    status: Literal[
        SlurmJobStatus.PENDING,
        SlurmJobStatus.RUNNING,
        SlurmJobStatus.COMPLETED,
        SlurmJobStatus.FAILED,
        SlurmJobStatus.CANCELLED,
    ]
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

    @model_validator(mode="after")
    def _complete_evidence(self) -> ClusterCollected:
        result = self.result
        code = result.job_exit_code if isinstance(result, SlurmBatchResult) else result.exit_code
        if code is None or result.collection_failure is not None:
            message = "collected result requires exit status and complete evidence"
            raise ValueError(message)
        if isinstance(result, SlurmBatchResult) and (
            not result.stages
            or any(
                (stage.exit_code is None and not stage.skipped)
                or stage.collection_failure is not None
                for stage in result.stages
            )
        ):
            message = "collected batch result requires complete stage exit statuses"
            raise ValueError(message)
        return self


ClusterSubmitOutcome: TypeAlias = (
    ClusterSubmitted | ClusterConflict | ClusterRejected | ClusterUnknown
)
ClusterInspectOutcome: TypeAlias = ClusterObservation | ClusterUnknown
ClusterCancelOutcome: TypeAlias = ClusterCancelRequested | ClusterUnknown
ClusterCollectOutcome: TypeAlias = ClusterCollected | ClusterUnknown
