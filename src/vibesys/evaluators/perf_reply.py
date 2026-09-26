"""Performance-measurement record types and the perf-evaluator reply shapes.

These are the pydantic models a performance-evaluator agent's structured
reply is constrained to (cross-loop ``PerfEvalResponse`` and the plain
loop's ``IssuePerfEvalResponse``), plus the measurement records they embed.
They live in ``evaluators`` rather than ``roles`` because they describe
measured data, not agent-turn wiring.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, FiniteFloat

from vibesys.schemas import PerfTrend


class LatencyStats(BaseModel):
    """Percentile breakdown for a latency metric (all values in milliseconds)."""

    mean_ms: FiniteFloat = Field(description="Mean latency in milliseconds.")
    p50_ms: FiniteFloat = Field(description="50th percentile (median) latency in milliseconds.")
    p90_ms: FiniteFloat = Field(description="90th percentile latency in milliseconds.")
    p95_ms: FiniteFloat = Field(description="95th percentile latency in milliseconds.")
    p99_ms: FiniteFloat = Field(description="99th percentile latency in milliseconds.")


class ThroughputStats(BaseModel):
    """Throughput metrics at a given load level."""

    request_throughput: FiniteFloat = Field(description="Requests per second.")
    token_throughput: FiniteFloat = Field(description="Output tokens per second.")


class LoadLevelMetrics(BaseModel):
    """Metrics collected at a single load level (request rate)."""

    target_rate: FiniteFloat = Field(description="Target request rate in req/s.")
    actual_rate: FiniteFloat = Field(description="Achieved request rate in req/s.")
    num_requests: int = Field(description="Total requests sent.")
    num_completed: int = Field(description="Requests that completed successfully.")
    num_failed: int = Field(description="Requests that failed.")
    duration: FiniteFloat = Field(description="Wall-clock duration in seconds.")
    throughput: ThroughputStats
    ttft: LatencyStats | None = Field(default=None, description="Time to first token stats.")
    tpot: LatencyStats | None = Field(default=None, description="Time per output token stats.")
    total_latency: LatencyStats | None = Field(
        default=None, description="End-to-end latency stats."
    )


class PerfMetrics(BaseModel):
    """Top-level metrics container. Supports single or multi-load runs."""

    load_levels: list[LoadLevelMetrics] = Field(description="One entry per load level tested.")
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Extensible — gpu_mem, batch_stats, etc."
    )


class PerfEvalResponse(BaseModel):
    """Structured response from the performance evaluator agent."""

    analysis: str = Field(
        description="What the evaluator observed — trends, bottlenecks, saturation points."
    )
    metrics: PerfMetrics = Field(
        description="Structured performance data collected from benchmark runs."
    )
    implementer_feedback: list[str] = Field(
        description="Bullet-point list of concrete optimization ideas for the implementer to try next iteration."
    )
    evaluator_feedback: list[str] = Field(
        description="Bullet-point list of notes for the next performance evaluator (e.g. benchmarking strategy, load levels to try, metrics to watch)."
    )
    throughput_trend: PerfTrend = Field(
        description="Whether throughput (req/s, tok/s) improved, regressed, or is mixed compared to the previous iteration."
    )
    latency_trend: PerfTrend = Field(
        description="Whether latency (TTFT, TPOT, total) improved, regressed, or is mixed compared to the previous iteration."
    )


class ProfilerSummary(BaseModel):
    """Structured summary from the profiler agent, shared with the orchestrator.

    Extends the core profiler response with an optional numeric ``perf_metric``
    the framework uses for regression detection across rounds.
    """

    analysis: str = Field(description="Detailed interpretation of the profile data.")
    bottlenecks: str = Field(description="Ranked bottlenecks with concrete numbers.")
    suggestions: str = Field(
        description=(
            "Advisory optimization or measurement suggestions tied to bottlenecks; "
            "they do not impose a planning prerequisite."
        )
    )
    perf_metric: FiniteFloat | None = Field(
        default=None,
        description=(
            "Uninverted primary performance metric collected during profiling. "
            "The configured primary objective determines whether lower or higher is better. "
            "Without configured objectives, scalar selection assumes higher is better. "
            "None when unavailable."
        ),
    )
    perf_unit: str | None = Field(
        default=None,
        description="Unit of perf_metric (e.g. 'req/s', 'tok/s'). None when perf_metric is None.",
    )
    metrics: dict[str, FiniteFloat] = Field(
        default_factory=dict,
        description=(
            "Optional multi-metric dict keyed by metric name (e.g. "
            "{'median_tok_per_sec': 42.1, 'p99_latency_ms': 87.3}). Used by "
            "the evolve loop's Pareto-frontier selection. Single-objective "
            "consumers (agent-loop plateau detection) ignore this field; "
            "they read perf_metric instead."
        ),
    )


class IssuePerfEvalResponse(BaseModel):
    """Structured response from the performance evaluator in the plain loop.

    Optimization ideas are filed as issues via the create_issue tool, NOT
    returned in this payload (cf. ``PerfEvalResponse.implementer_feedback``).
    """

    analysis: str = Field(
        description="What the evaluator observed — trends, bottlenecks, saturation points."
    )
    metrics: PerfMetrics = Field(
        description="Structured performance data collected from benchmark runs."
    )
    evaluator_feedback: list[str] = Field(
        description="Bullet-point list of notes for the next performance evaluator (e.g. benchmarking strategy, load levels to try, metrics to watch)."
    )
    new_issue_ids: list[int] = Field(
        default_factory=list, description="IDs of issues filed via create_issue this round."
    )
    throughput_trend: PerfTrend = Field(
        description="Whether throughput (req/s, tok/s) improved, regressed, or is mixed compared to the previous iteration."
    )
    latency_trend: PerfTrend = Field(
        description="Whether latency (TTFT, TPOT, total) improved, regressed, or is mixed compared to the previous iteration."
    )


__all__ = [
    "IssuePerfEvalResponse",
    "LatencyStats",
    "LoadLevelMetrics",
    "PerfEvalResponse",
    "PerfMetrics",
    "ProfilerSummary",
    "ThroughputStats",
]
