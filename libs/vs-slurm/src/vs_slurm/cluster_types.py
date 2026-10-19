"""Validated observations for the cluster job interface."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, model_validator

from .errors import SlurmError
from .runner import (
    _MAX_PROCESS_EXIT_CODE,
    SlurmBatchHandle,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmJobHandle,
    SlurmJobResult,
    SlurmJobStatus,
    SlurmPhase,
)
from .scheduler_states import JobLife, phase_of, status_of

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
    model_config = ConfigDict(
        extra="forbid", frozen=True, arbitrary_types_allowed=True, strict=True
    )


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
    """Ambiguous observation with identity and preserved evidence.

    ``job_absent`` is True only on an inspection whose reads all succeeded and
    found no job under the operation's name: none is queued or running now,
    though a submission still in flight may yet start one. It is False for
    every other Unknown (a failed transport call, a scheduler state it cannot
    read, an ambiguous name), whose job may be live. A caller that would end
    an attempt as jobless, and so let the next attempt start a job of its own,
    does so only when it is True (``take_over_jobless``).
    """

    kind: Literal["unknown"] = "unknown"
    operation_id: str | None
    reason: str
    job_id: str | None = None
    result: ClusterResult | None = None
    job_absent: bool = False

    @model_validator(mode="after")
    def _absent_names_no_job(self) -> ClusterUnknown:
        if self.job_absent and (self.job_id is not None or self.result is not None):
            message = "an Unknown that found no job names no job and carries no result"
            raise ValueError(message)
        return self


class ClusterObservation(_Outcome):
    """Scheduler observation without lifecycle policy."""

    kind: Literal["observed"] = "observed"
    operation_id: str | None
    job_id: str
    # What the scheduler's readings made of the job. ``status``, ``phase`` and
    # ``attempt`` project it; a higher attempt makes a drop from RUNNING back to
    # PENDING a requeue, not a regression. Never Unknown: that is ClusterUnknown.
    life: JobLife
    pending_reason: str | None = None
    estimated_start: str | None = None
    handle: ClusterHandle | None = None
    # For a batch that is computing: how many of its stages have finished, so the
    # stage at this index is the one running. None when not observed (not a
    # batch, not computing, or the read failed): callers must not assume stage 0.
    completed_stages: int | None = None

    @model_validator(mode="after")
    def _known_life(self) -> ClusterObservation:
        if self.life.kind == "unknown":
            message = "an observation needs a known life; an unknown one is ClusterUnknown"
            raise ValueError(message)
        return self

    @property
    def status(self) -> SlurmJobStatus:
        """The public status of the life."""
        return status_of(self.life)

    @property
    def phase(self) -> SlurmPhase:
        """The phase of the life."""
        return phase_of(self.life)

    @property
    def attempt(self) -> int:
        """The scheduler attempt of the life."""
        return self.life.attempt


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


class ClusterCall(StrEnum):
    """What one transport call to the cluster does, as a caller budgeting its time counts it.

    Each call costs up to one transport timeout (``SlurmConfig.transport_timeout_seconds``).
    """

    SCHEDULER_READ = "scheduler_read"
    """A ``squeue`` or ``sacct`` read."""
    SCANCEL = "scancel"
    """The ``scancel`` itself: the job is cancelled once it returns."""
    REMOTE_FILE = "remote_file"
    """Any other call: a remote operation record read or written."""


ANOTHER_JOB: Final = "the observation passed to cancel is of another job"
"""Why ``Cluster.cancel`` given a reading of another job than its target cancels nothing."""

OBSERVED_CANCEL_CALLS: Final = 1
"""The transport calls ``Cluster.cancel`` makes up to and including its scancel, given ``observed``.

When ``observed`` shows the job queued or running, the scancel is the cancel's
first transport call; the bookkeeping that follows it does not delay it. A
caller that must have the job cancelled by a deadline (a withdrawal, whose mark
expires) reserves this many calls. The contract suite asserts it for every
implementation.
"""


def observation_names(
    target: ClusterTarget, observed: ClusterObservation, *, by_job_id: bool
) -> bool:
    """Whether ``observed`` is a reading of the job ``target`` names. Pure."""
    if isinstance(target, str):
        return observed.job_id == target if by_job_id else observed.operation_id == target
    handle = target.job if isinstance(target, SlurmBatchHandle) else target
    return observed.job_id == handle.job_id


def live_observation(observed: ClusterObservation | None) -> ClusterObservation | None:
    """``observed`` when it shows a job a scancel still stops, queued or running; else None. Pure."""
    if observed is None or observed.phase not in {SlurmPhase.PENDING, SlurmPhase.RUNNING}:
        return None
    return observed
