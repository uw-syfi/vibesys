"""Strict contracts the issue-queue orchestration exposes: the load levels and its agent replies.

Replies are the only way an agent changes the board. A judge may file at most one bug, of the
type its reply schema lists, so a reply that files more is not a valid reply. The performance
evaluator answers in the domain's own vocabulary (serving load levels or domain-neutral
measurements); the search files at most its configured number of the issues it names. A role
that evaluates answers with its result or with a wait for its evaluations; the role declaration
says so and core wraps the reply.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from vibesys.hypothesis import DecidedFrom
from vs_core.api.search import RevisionReply

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoadLevel(_StrictModel):
    """One policy-selected benchmark workload."""

    rate: PositiveInt
    duration: PositiveInt
    max_tokens: PositiveInt


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


class PerformanceMeasurement(_StrictModel):
    """One named benchmark result, with the workload and unit the benchmark reported."""

    name: str = Field(min_length=1)
    value: float
    unit: str | None = Field(default=None, min_length=1)
    workload: str | None = Field(default=None, min_length=1)

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            message = "performance measurements must be finite"
            raise ValueError(message)
        return value


class PerfMetrics(_StrictModel):
    """All measurements returned by one LLM-serving performance turn."""

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


class GenericPerfMetrics(_StrictModel):
    """Domain-neutral measurements returned by one performance turn."""

    measurements: tuple[PerformanceMeasurement, ...] = ()
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


class IssueImplementerResult(_StrictModel, RevisionReply):
    """Structured result of implementing one issue; core checkpoints the workspace it leaves."""

    summary: str
    files_touched: tuple[str, ...] = ()
    self_check: str


class IssueJudgeResult(_StrictModel):
    """Structured correctness review of one issue attempt, with at most one filed bug."""

    analysis: str
    feedback: str
    verdict: Annotated[
        Literal["pass", "fail"],
        DecidedFrom("issue.description", "summary", "self_check", "evaluations"),
    ]
    new_issues: Annotated[tuple[NewBug, ...], DecidedFrom("board")] = Field(
        default=(), max_length=1
    )


class IssuePerfEvalResult(_StrictModel):
    """Structured performance assessment and the issues it files.

    A serving task reports load levels and the throughput and latency trends; any other task
    reports named measurements and a primary (and optionally a secondary) metric trend. Exactly
    one of the two vocabularies is used, and the prompt of the run's domain says which.
    """

    analysis: str
    metrics: PerfMetrics | GenericPerfMetrics
    evaluator_feedback: tuple[str, ...]
    new_issues: Annotated[tuple[NewIssue, ...], DecidedFrom("board", "max_new_issues")] = ()
    throughput_trend: Annotated[PerformanceTrend | None, DecidedFrom("prior")] = None
    latency_trend: Annotated[PerformanceTrend | None, DecidedFrom("prior")] = None
    primary_metric_trend: Annotated[PerformanceTrend | None, DecidedFrom("prior")] = None
    secondary_metric_trend: Annotated[PerformanceTrend | None, DecidedFrom("prior")] = None

    @model_validator(mode="after")
    def _one_vocabulary(self) -> Self:
        serving = isinstance(self.metrics, PerfMetrics)
        own = (
            (self.throughput_trend, self.latency_trend) if serving else (self.primary_metric_trend,)
        )
        foreign = (
            (self.primary_metric_trend, self.secondary_metric_trend)
            if serving
            else (self.throughput_trend, self.latency_trend)
        )
        if any(trend is None for trend in own) or any(trend is not None for trend in foreign):
            vocabulary = (
                "throughput_trend and latency_trend"
                if serving
                else "primary_metric_trend (and optionally secondary_metric_trend)"
            )
            message = f"these metrics are reported with {vocabulary} only"
            raise ValueError(message)
        return self

    @property
    def primary_trend(self) -> PerformanceTrend:
        """The trend of the headline metric (throughput for a serving task)."""
        trend = self.throughput_trend or self.primary_metric_trend
        if trend is None:
            message = "a validated evaluation names its headline trend"
            raise ValueError(message)
        return trend

    @property
    def secondary_trend(self) -> PerformanceTrend | None:
        """The trend of the second metric (latency for a serving task), when there is one."""
        return self.latency_trend or self.secondary_metric_trend


__all__ = [
    "GenericPerfMetrics",
    "IssueImplementerResult",
    "IssueJudgeResult",
    "IssuePerfEvalResult",
    "LatencyStats",
    "LoadLevel",
    "LoadLevelMetrics",
    "NewBug",
    "NewIssue",
    "PerfMetrics",
    "PerformanceMeasurement",
    "PerformanceTrend",
    "ThroughputStats",
]
