"""Strict policy contracts owned by the issue-queue orchestration."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from vs_issue_tracker.api import IssueTrackerConfig

if TYPE_CHECKING:
    from vs_issue_tracker.api import Issue

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
IssueQueuePhase = Literal["implementer", "judge", "perf_eval"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoadLevel(_StrictModel):
    """One policy-selected benchmark workload."""

    rate: PositiveInt
    duration: PositiveInt
    max_tokens: PositiveInt


class IssueQueueOptions(_StrictModel):
    """Complete configurable policy for one issue-queue run."""

    modality: str | None = None
    max_rounds: PositiveInt
    max_attempts_per_issue: PositiveInt
    max_issues_per_perf_eval: PositiveInt
    load_levels: tuple[LoadLevel, ...] | None = None
    tracker: IssueTrackerConfig = Field(default_factory=IssueTrackerConfig.local)

    @field_validator("load_levels", mode="before")
    @classmethod
    def _freeze_load_levels(cls, value: object) -> object:
        """Accept the existing input-bundle list while storing an immutable tuple."""
        return tuple(value) if isinstance(value, list) else value


PerformanceTrend = Literal["improved", "regressed", "mixed"]


class LatencyStats(_StrictModel):
    """Latency percentiles in milliseconds."""

    mean_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float


class ThroughputStats(_StrictModel):
    """Request and token throughput for one workload."""

    request_throughput: float
    token_throughput: float


class LoadLevelMetrics(_StrictModel):
    """Measured results from one load level."""

    target_rate: float
    actual_rate: float
    num_requests: NonNegativeInt
    num_completed: NonNegativeInt
    num_failed: NonNegativeInt
    duration: float
    throughput: ThroughputStats
    ttft: LatencyStats | None = None
    tpot: LatencyStats | None = None
    total_latency: LatencyStats | None = None


class PerfMetrics(_StrictModel):
    """All measurements returned by one performance turn."""

    load_levels: tuple[LoadLevelMetrics, ...]
    extra: dict[str, JsonValue] = Field(default_factory=dict)


class IssueImplementerResponse(_StrictModel):
    """Structured result of implementing one issue."""

    issue_id: int
    summary: str
    files_touched: tuple[str, ...] = ()
    self_check: str


class IssueJudgeResponse(_StrictModel):
    """Structured correctness review of one issue attempt."""

    issue_id: int
    analysis: str
    feedback: str
    verdict: Literal["pass", "fail"]
    new_issues_filed: tuple[int, ...] = ()


class IssuePerfEvalResponse(_StrictModel):
    """Structured performance assessment and filed follow-up issues."""

    analysis: str
    metrics: PerfMetrics
    evaluator_feedback: tuple[str, ...]
    new_issue_ids: tuple[int, ...] = ()
    throughput_trend: PerformanceTrend
    latency_trend: PerformanceTrend


class PerformanceRecord(_StrictModel):
    """Durable result that prevents repeating a paid performance turn."""

    iteration: PositiveInt
    throughput_trend: PerformanceTrend
    latency_trend: PerformanceTrend
    metrics: dict[str, JsonValue]
    new_issue_ids: tuple[PositiveInt, ...] = ()

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        def check(item: JsonValue) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                message = "performance metrics must contain only finite numbers"
                raise ValueError(message)
            if isinstance(item, list):
                for child in item:
                    check(child)
            elif isinstance(item, dict):
                for child in item.values():
                    check(child)

        check(value)
        return value

    @field_validator("new_issue_ids")
    @classmethod
    def _unique_issue_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)):
            message = "new_issue_ids must be unique"
            raise ValueError(message)
        return value


class IssueQueueState(_StrictModel):
    """One strict aggregate for the resumable cursor and performance history."""

    version: Literal[1] = 1
    round_idx: NonNegativeInt = 0
    phase: IssueQueuePhase = "implementer"
    current_issue_id: PositiveInt | None = None
    bootstrap_done: bool = False
    performance: tuple[PerformanceRecord, ...] = ()

    @model_validator(mode="after")
    def _consistent_cursor_and_history(self) -> Self:
        if self.phase == "judge" and self.current_issue_id is None:
            message = "judge phase requires current_issue_id"
            raise ValueError(message)
        if self.phase == "perf_eval" and self.current_issue_id is not None:
            message = "perf_eval phase cannot reference current_issue_id"
            raise ValueError(message)
        iterations = [record.iteration for record in self.performance]
        if iterations != sorted(iterations) or len(iterations) != len(set(iterations)):
            message = "performance iterations must be strictly increasing"
            raise ValueError(message)
        return self

    def transition(
        self,
        *,
        round_idx: int,
        phase: IssueQueuePhase,
        current_issue_id: int | None,
    ) -> IssueQueueState:
        """Return one validated cursor transition."""
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "round_idx": round_idx,
                "phase": phase,
                "current_issue_id": current_issue_id,
            },
            strict=True,
        )

    def mark_bootstrapped(self) -> IssueQueueState:
        """Return the validated initialized aggregate."""
        return type(self).model_validate(
            {**self.model_dump(mode="python"), "bootstrap_done": True},
            strict=True,
        )

    def append_performance(self, record: PerformanceRecord) -> IssueQueueState:
        """Append one validated, strictly ordered performance record."""
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "performance": (*self.performance, record),
            },
            strict=True,
        )


class IssueToolPolicy(_StrictModel):
    """Dynamic create policy consumed by the fixed issue-board tool server."""

    creator: Literal["judge", "perf_eval"]
    iteration: PositiveInt
    cap: PositiveInt
    allowed_types: tuple[Literal["bug", "feature", "perf"], ...]


def latest_judge_review(issue: Issue) -> dict[str, Any] | None:
    """Return the most recent failed judge feedback for an implementation retry."""
    for event in reversed(issue.history):
        if event.actor != "judge" or "->" not in event.action:
            continue
        payload = event.payload or {}
        feedback = str(payload.get("feedback") or event.note or "").strip()
        analysis = str(payload.get("analysis") or "").strip()
        if feedback or analysis:
            return {
                "feedback": feedback,
                "analysis": analysis,
                "iteration": event.iteration,
            }
        return None
    return None


__all__ = [
    "IssueImplementerResponse",
    "IssueJudgeResponse",
    "IssuePerfEvalResponse",
    "IssueQueueOptions",
    "IssueQueuePhase",
    "IssueQueueState",
    "IssueToolPolicy",
    "LoadLevel",
    "PerformanceRecord",
    "PerformanceTrend",
    "latest_judge_review",
]
