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
    """Submission failure classification; unknown never proves workload rejection."""

    WORKLOAD = "workload"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"
