"""Strict contracts the issue-queue orchestration exposes: its options and its agent replies.

Replies are the only way an agent changes the board. A judge may file at most one bug and
the performance evaluator at most ``max_issues_per_perf_eval`` issues, of types the reply
schema lists, so a reply that exceeds either is not a valid reply. A role that evaluates
answers with its result or with a wait for its evaluations (`Waitable`).
"""

import math
from functools import cache
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    field_validator,
)

from vs_core.api import Waitable
from vs_issue_tracker.api import IssueTrackerConfig

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


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
    # Issues worked in one round before the performance evaluation runs; the rest wait for the
    # next round. None means twice the evaluator's cap plus two, so one round always has room
    # for the bootstrap issue, the issues the evaluator filed and a bug or two the judge found.
    max_issues_per_round: PositiveInt | None = None
    # Seconds to wait before asking an agent again after the provider connection dropped its
    # turn; doubles with each further drop of the same turn.
    turn_drop_backoff_seconds: Annotated[float, Field(gt=0, le=600)] = 5.0
    load_levels: tuple[LoadLevel, ...] | None = None
    tracker: IssueTrackerConfig = Field(default_factory=IssueTrackerConfig.local)

    @field_validator("load_levels", mode="before")
    @classmethod
    def _freeze_load_levels(cls, value: object) -> object:
        """Accept the existing input-bundle list while storing an immutable tuple."""
        return tuple(value) if isinstance(value, list) else value

    @property
    def issues_per_round(self) -> int:
        """The bound on issues worked per round."""
        if self.max_issues_per_round is not None:
            return self.max_issues_per_round
        return 2 * self.max_issues_per_perf_eval + 2


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

    @field_validator("extra")
    @classmethod
    def _finite_extra(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
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


class NewBug(_StrictModel):
    """A bug the judge found that is unrelated to the issue under review."""

    type: Literal["bug"]
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)


class NewIssue(_StrictModel):
    """An issue the performance evaluator files."""

    type: Literal["bug", "feature", "perf"]
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)


class IssueImplementerResult(_StrictModel):
    """Structured result of implementing one issue."""

    summary: str
    files_touched: tuple[str, ...] = ()
    self_check: str


class IssueJudgeResult(_StrictModel):
    """Structured correctness review of one issue attempt, with at most one filed bug."""

    analysis: str
    feedback: str
    verdict: Literal["pass", "fail"]
    new_issues: tuple[NewBug, ...] = Field(default=(), max_length=1)


class IssuePerfEvalResult(_StrictModel):
    """Structured performance assessment and the issues it files."""

    analysis: str
    metrics: PerfMetrics
    evaluator_feedback: tuple[str, ...]
    new_issues: tuple[NewIssue, ...] = ()
    throughput_trend: PerformanceTrend
    latency_trend: PerformanceTrend


class ImplementerReplyModel(RootModel[Waitable[IssueImplementerResult]]):
    """An implementer turn ends with its result or a wait for evaluations."""


class JudgeReplyModel(RootModel[Waitable[IssueJudgeResult]]):
    """A judge turn ends with its verdict or a wait for evaluations."""


@cache
def perf_result_model(cap: int) -> type[IssuePerfEvalResult]:
    """The evaluator's result type, whose ``new_issues`` holds at most ``cap`` issues."""

    class CappedIssuePerfEvalResult(IssuePerfEvalResult):
        model_config = ConfigDict(extra="forbid", strict=True, title=f"IssuePerfEvalResultCap{cap}")
        new_issues: tuple[NewIssue, ...] = Field(default=(), max_length=cap)

    return CappedIssuePerfEvalResult


__all__ = [
    "ImplementerReplyModel",
    "IssueImplementerResult",
    "IssueJudgeResult",
    "IssuePerfEvalResult",
    "IssueQueueOptions",
    "JudgeReplyModel",
    "LatencyStats",
    "LoadLevel",
    "LoadLevelMetrics",
    "NewBug",
    "NewIssue",
    "PerfMetrics",
    "PerformanceTrend",
    "ThroughputStats",
    "perf_result_model",
]
