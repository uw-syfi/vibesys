"""Structured profiler reply consumed by orchestration policies."""

from __future__ import annotations

from pydantic import BaseModel, Field, FiniteFloat


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


__all__ = ["ProfilerSummary"]
