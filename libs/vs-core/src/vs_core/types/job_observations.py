"""Evaluation facts shared with records without importing lifecycle state."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .common import ContractValidationError, Count, ResourceId, Seconds, Value


class JobProgress(Value):
    """Current nonterminal job progress, retaining absent measurements as None.

    Sequence and time must match the carrying observation. Older observations
    cannot replace newer progress; changed payload at equal sequence conflicts.
    Stage IDs must belong to the owner's validated stage registry.
    """

    observation_sequence: Count
    observed_at: Seconds
    state: Literal["pending", "running", "unknown"]
    stage_id: str | None = Field(default=None, min_length=1)
    queued_s: Seconds | None = None
    ran_s: Seconds | None = None
    pending_reason: str | None = None
    estimated_start_at: Seconds | None = None


class WaitEnd(StrEnum):
    """What ended a wait on a job: every wait ends by exactly one of these, or by a stop.

    Closed. The bound of every wait is a time (``_wait_clock.Bound``), never absent; this
    names the rule that set it.
    """

    LIMIT = "limit"
    """The work's stated limit passed, counted from its job's start (plus the queue time
    its fresh pending facts were credited)."""
    QUEUE_BUDGET = "queue_budget"
    """The job waited in its queue for the run's whole ``queue_wait_budget``."""
    RUN_END = "run_end"
    """The run's own deadline: only queue time can carry a limit past it."""


class JobTimeout(Value):
    """Frozen progress of one unfinished dependency when the bound that ended it passed.

    A wait ends a job either with the wait itself (its earliest bound) or alone, when the
    job's own queue budget runs out while its siblings go on (``QUEUE_BUDGET``).
    """

    resource_id: ResourceId
    progress: JobProgress | None = None
    by: WaitEnd
    """The bound that ended this job's part of the wait."""
    at: Seconds
    """When that bound passed."""


class TimedOut(Value):
    """Immutable wait-all timeout evidence, separate from job termination.

    Freeze before applying observations at or beyond the deadline. unfinished
    names each unfinished dependency once, in continuation order. Late results
    may resolve cleanup but never rewrite this value or resume authorization.
    Waiting, inspection and timeout authorization create no execution charge.
    """

    kind: Literal["timed_out"] = "timed_out"
    deadline_at: Seconds
    reached_at: Seconds
    by: WaitEnd = WaitEnd.LIMIT
    """Which bound ``deadline_at`` was: the wait's limit, a job's queue budget, or the run's end."""
    unfinished: tuple[JobTimeout, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_deadline_manifest(self) -> TimedOut:
        """Reject premature timeout and duplicate unfinished dependencies."""
        if self.reached_at < self.deadline_at:
            raise ContractValidationError("reached_at", "precedes deadline_at")
        if any(job.at > self.reached_at for job in self.unfinished):
            raise ContractValidationError("unfinished", "a job's bound follows reached_at")
        ids = tuple(job.resource_id for job in self.unfinished)
        if len(set(ids)) != len(ids):
            raise ContractValidationError("unfinished", "duplicate resource ID")
        return self


class MeasurementFailure(StrEnum):
    """Submission failure classification; unknown never proves workload rejection.

    ``AMBIGUOUS`` is a failure the evidence cannot assign to the candidate or to the
    machinery. The backend decides how often it measures such a failure again, within its
    declared bound and core's hard cap; what reaches core is final.

    ``RUN_ENVIRONMENT_UNAVAILABLE`` is a measurement the run environment refused because
    it can run no such work at all (a local Slurm root its probe could not prove shared).
    It is nobody's candidate's fault, and no retry can succeed, so it is never retried.
    """

    WORKLOAD = "workload"
    INFRASTRUCTURE = "infrastructure"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"
    RUN_ENVIRONMENT_UNAVAILABLE = "run_environment_unavailable"

    @property
    def retryable(self) -> bool:
        """Whether the failure is not proven the candidate's, so core may admit another submission."""
        return self in (MeasurementFailure.INFRASTRUCTURE, MeasurementFailure.AMBIGUOUS)
