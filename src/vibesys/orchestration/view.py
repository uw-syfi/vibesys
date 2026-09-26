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
    """Lifecycle status `vibesys.api` can report for a run.

    Deliberately a narrower, separately-owned vocabulary from `EventStatus`
    (not a re-export): `RunQuery.view` on a live session reports `ACTIVE`
    while its loop is running, then `COMPLETED`/`FAILED` from the same
    `RUN_FINISHED`/`RUN_FAILED` transition `create_session` already emits as
    `EventStatus.COMPLETED`/`EventStatus.FAILED` (see `session.py`). `RunStore`
    projects a run from its durable files alone, which carry no lifecycle
    field, so it always reports `UNKNOWN` rather than guessing whether the
    process that wrote them is still attached.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class RoundSummary(BaseModel):
    """One round, in the strategy-agnostic shape the host needs to derive events.

    Every strategy projector that has a round concept populates this (today
    only the agent read model does; evolve/issue_queue leave `RunView.rounds`
    empty). The host diffs these typed fields to derive `ROUND_FINISHED`
    without reading any policy-owned projection shape.
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

    `rounds` and `experiment_revision` are typed, strategy-agnostic fields a
    projector populates alongside `projection` so the host
    (`orchestration.runtime`) can derive `ROUND_FINISHED`/`EXPERIMENTS_CHANGED`
    by diffing them, without depending on any one policy's projection shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    status: RunStatus
    projection: dict[str, JsonValue] | None = None
    rounds: tuple[RoundSummary, ...] = ()
    experiment_revision: int | None = None
