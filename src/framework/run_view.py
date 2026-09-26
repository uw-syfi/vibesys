"""Generic orchestration run results and views."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue


class RunResult(BaseModel):
    """Terminal outcome of one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    succeeded: bool


class RunStatus(StrEnum):
    """Lifecycle status for a run projection.

    ``UNKNOWN`` is used when durable run data contains no reliable lifecycle
    field; consumers must not infer whether the writing process is still live.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class RoundSummary(BaseModel):
    """One round, in the strategy-agnostic shape the host needs to derive events.

    A projector fills this when its policy has a round concept. The execution
    host diffs these fields to derive round events without reading
    policy-owned projection shapes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int
    status: Literal["completed", "failed"]
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"] | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    profile_skipped: bool = False


class RunView(BaseModel):
    """Read-only run identity, lifecycle, and a policy-owned JSON projection.

    The selected orchestration owns the payload schema. An absent projection
    means that the policy has no persisted read model or is unavailable.

    ``rounds`` and ``experiment_revision`` are typed, strategy-agnostic fields
    a projector populates alongside ``projection`` so the execution host can
    derive events without depending on a policy's projection shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    status: RunStatus
    projection: dict[str, JsonValue] | None = None
    rounds: tuple[RoundSummary, ...] = ()
    experiment_revision: int | None = None
