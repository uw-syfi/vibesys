"""Validation tests for shared structured agent-response schemas."""

import pytest
from pydantic import ValidationError

from vibesys.schemas import (
    LatencyStats,
    LoadLevelMetrics,
    ProfilerSummary,
    SingleAgentRoundResponse,
    ThroughputStats,
    Verdict,
)
from vibesys.server.protocol import PerformanceRound


def _profiler_summary(
    *,
    perf_metric: float | None = None,
    metrics: dict[str, float] | None = None,
) -> ProfilerSummary:
    return ProfilerSummary(
        analysis="analysis",
        bottlenecks="bottlenecks",
        suggestions="suggestions",
        perf_metric=perf_metric,
        metrics=metrics or {},
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_profiler_summary_rejects_non_finite_perf_metric(value):
    with pytest.raises(ValidationError, match="finite number"):
        _profiler_summary(perf_metric=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_profiler_summary_rejects_non_finite_multi_objective_metric(value):
    with pytest.raises(ValidationError, match="finite number"):
        _profiler_summary(metrics={"throughput": value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_single_agent_response_rejects_non_finite_perf_metric(value):
    with pytest.raises(ValidationError, match="finite number"):
        SingleAgentRoundResponse(
            summary="summary",
            expected_behavior="expected behavior",
            self_review="self review",
            feedback="",
            verdict=Verdict.PASS,
            bottlenecks="bottlenecks",
            suggestions="suggestions",
            profile_analysis="analysis",
            perf_metric=value,
            perf_unit="req/s",
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_performance_stats_reject_non_finite_values(value):
    with pytest.raises(ValidationError, match="finite number"):
        LatencyStats(
            mean_ms=value,
            p50_ms=1.0,
            p90_ms=1.0,
            p95_ms=1.0,
            p99_ms=1.0,
        )

    with pytest.raises(ValidationError, match="finite number"):
        ThroughputStats(request_throughput=value, token_throughput=1.0)

    with pytest.raises(ValidationError, match="finite number"):
        LoadLevelMetrics(
            target_rate=value,
            actual_rate=1.0,
            num_requests=1,
            num_completed=1,
            num_failed=0,
            duration=1.0,
            throughput=ThroughputStats(request_throughput=1.0, token_throughput=1.0),
        )

    with pytest.raises(ValidationError, match="finite number"):
        PerformanceRound(
            round=1,
            perf_metric=value,
            perf_unit="req/s",
            passed=True,
        )
