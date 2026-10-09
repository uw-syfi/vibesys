"""Evaluation facts shared with intents without importing lifecycle state."""

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


class JobTimeout(Value):
    """Frozen progress of one unfinished dependency at its wait deadline."""

    resource_id: ResourceId
    progress: JobProgress | None = None


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
    unfinished: tuple[JobTimeout, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_deadline_manifest(self) -> TimedOut:
        """Reject premature timeout and duplicate unfinished dependencies."""
        if self.reached_at < self.deadline_at:
            raise ContractValidationError("reached_at", "precedes deadline_at")
        ids = tuple(job.resource_id for job in self.unfinished)
        if len(set(ids)) != len(ids):
            raise ContractValidationError("unfinished", "duplicate resource ID")
        return self


class MeasurementFailure(StrEnum):
    """Submission failure classification; unknown never proves workload rejection.

    ``AMBIGUOUS`` is a failure the evidence cannot assign to the candidate or to the
    machinery. It is submitted once more (see ``may_resubmit``); a second one is final.
    """

    WORKLOAD = "workload"
    INFRASTRUCTURE = "infrastructure"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        """Whether a submission with this failure may be submitted again (within its bound)."""
        return self in (MeasurementFailure.INFRASTRUCTURE, MeasurementFailure.AMBIGUOUS)


# An ambiguous failure is measured at most this many times in all: once, then once more.
AMBIGUOUS_SUBMISSION_LIMIT = 2


def may_resubmit(failure: MeasurementFailure | None, *, submissions: int, limit: int) -> bool:
    """Whether a submission that ended with ``failure`` may be submitted again.

    ``submissions`` counts those already made for the measurement and ``limit`` is its
    submission bound. Infrastructure failures retry up to the bound, ambiguous ones up to
    ``AMBIGUOUS_SUBMISSION_LIMIT`` (never beyond the bound), and nothing else retries.
    Core's budget and every strategy's retry decision call this one rule.
    """
    if failure is None or not failure.retryable:
        return False
    if failure is MeasurementFailure.AMBIGUOUS:
        limit = min(limit, AMBIGUOUS_SUBMISSION_LIMIT)
    return submissions < limit
