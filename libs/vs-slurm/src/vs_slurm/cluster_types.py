"""Validated observations for the cluster job interface."""

from __future__ import annotations

import re
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, model_validator

from .runner import (
    _MAX_PROCESS_EXIT_CODE,
    SlurmBatchHandle,
    SlurmBatchResult,
    SlurmBatchStageResult,
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


def _valid_exit_status(value: int | None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_PROCESS_EXIT_CODE
    )


def collection_problem(result: ClusterResult) -> str | None:
    """Classify incomplete or malformed terminal evidence without inferring success."""
    code = result.job_exit_code if isinstance(result, SlurmBatchResult) else result.exit_code
    if not _valid_exit_status(code):
        return "missing or malformed allocation exit status"
    if result.collection_failure is not None:
        return result.collection_failure
    if isinstance(result, SlurmBatchResult):
        if not result.stages:
            return "missing stage evidence"
        for stage in result.stages:
            problem = _stage_problem(stage)
            if problem is not None:
                return problem
    return None


def _stage_problem(stage: SlurmBatchStageResult) -> str | None:
    if stage.collection_failure is not None:
        return stage.collection_failure
    if stage.skipped:
        return (
            "skipped stage has a contradictory exit status" if stage.exit_code is not None else None
        )
    return None if _valid_exit_status(stage.exit_code) else "missing or malformed stage exit status"


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
        problem = collection_problem(self.result)
        if problem is not None:
            raise ValueError(problem)
        return self


ClusterSubmitOutcome: TypeAlias = (
    ClusterSubmitted | ClusterConflict | ClusterRejected | ClusterUnknown
)
ClusterInspectOutcome: TypeAlias = ClusterObservation | ClusterUnknown
ClusterCancelOutcome: TypeAlias = ClusterCancelRequested | ClusterUnknown
ClusterCollectOutcome: TypeAlias = ClusterCollected | ClusterUnknown
