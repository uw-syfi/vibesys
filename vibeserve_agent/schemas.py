"""Pydantic schemas for structured agent responses.

Every Pydantic model the framework uses to constrain an LLM's JSON
output lives here, organized by purpose:

  - Enums:                Verdict, PerfTrend
  - Implementer / Judge:  ImplementerResponse, JudgeResponse
                          IssueImplementerResponse, IssueJudgeResponse
                          (the "Issue*" variants are the plain loop's
                           specialisation — they carry an ``issue_id``)
  - Performance metrics:  LatencyStats, ThroughputStats, LoadLevelMetrics,
                          PerfMetrics, PerfEvalResponse,
                          IssuePerfEvalResponse
  - Profiler:             ProfilerResponse, ProfilerSummary
  - Orchestrator (agent loop):
                          PreRoundDecision, OrchestratorPlan
  - Mutator (evolve loop):
                          MutatorResponse

This module has no local imports, so templates and tests can pull
schemas in without dragging in the rest of the agent runtime.
"""

from enum import Enum

from pydantic import BaseModel, Field


# ===========================================================================
# Enums
# ===========================================================================


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class PerfTrend(str, Enum):
    IMPROVED = "improved"
    REGRESSED = "regressed"
    MIXED = "mixed"


# ===========================================================================
# Implementer / Judge — cross-loop
# ===========================================================================


class ImplementerResponse(BaseModel):
    """Structured response from the implementer agent."""

    summary: str = Field(description="What was implemented or changed this iteration.")
    expected_behavior: str = Field(description="What behavior is expected (e.g. 'server starts on port 8000, /health returns 200').")


class JudgeResponse(BaseModel):
    """Structured response from the judge agent."""

    analysis: str = Field(description="Detailed analysis of the implementation covering correctness, completeness, dependencies, tests, and code quality.")
    feedback: str = Field(description="Specific actionable feedback for the implementer. Empty string if passing.")
    verdict: Verdict = Field(description="PASS if all criteria are met, FAIL otherwise.")


# ===========================================================================
# Performance metrics + perf-eval response (cross-loop)
# ===========================================================================


class LatencyStats(BaseModel):
    """Percentile breakdown for a latency metric (all values in milliseconds)."""

    mean_ms: float = Field(description="Mean latency in milliseconds.")
    p50_ms: float = Field(description="50th percentile (median) latency in milliseconds.")
    p90_ms: float = Field(description="90th percentile latency in milliseconds.")
    p95_ms: float = Field(description="95th percentile latency in milliseconds.")
    p99_ms: float = Field(description="99th percentile latency in milliseconds.")


class ThroughputStats(BaseModel):
    """Throughput metrics at a given load level."""

    request_throughput: float = Field(description="Requests per second.")
    token_throughput: float = Field(description="Output tokens per second.")


class LoadLevelMetrics(BaseModel):
    """Metrics collected at a single load level (request rate)."""

    target_rate: float = Field(description="Target request rate in req/s.")
    actual_rate: float = Field(description="Achieved request rate in req/s.")
    num_requests: int = Field(description="Total requests sent.")
    num_completed: int = Field(description="Requests that completed successfully.")
    num_failed: int = Field(description="Requests that failed.")
    duration: float = Field(description="Wall-clock duration in seconds.")
    throughput: ThroughputStats
    ttft: LatencyStats | None = Field(default=None, description="Time to first token stats.")
    tpot: LatencyStats | None = Field(default=None, description="Time per output token stats.")
    total_latency: LatencyStats | None = Field(default=None, description="End-to-end latency stats.")


class PerfMetrics(BaseModel):
    """Top-level metrics container. Supports single or multi-load runs."""

    load_levels: list[LoadLevelMetrics] = Field(description="One entry per load level tested.")
    extra: dict = Field(default_factory=dict, description="Extensible — gpu_mem, batch_stats, etc.")


class PerfEvalResponse(BaseModel):
    """Structured response from the performance evaluator agent."""

    analysis: str = Field(description="What the evaluator observed — trends, bottlenecks, saturation points.")
    metrics: PerfMetrics = Field(description="Structured performance data collected from benchmark runs.")
    implementer_feedback: list[str] = Field(description="Bullet-point list of concrete optimization ideas for the implementer to try next iteration.")
    evaluator_feedback: list[str] = Field(description="Bullet-point list of notes for the next performance evaluator (e.g. benchmarking strategy, load levels to try, metrics to watch).")
    throughput_trend: PerfTrend = Field(description="Whether throughput (req/s, tok/s) improved, regressed, or is mixed compared to the previous iteration.")
    latency_trend: PerfTrend = Field(description="Whether latency (TTFT, TPOT, total) improved, regressed, or is mixed compared to the previous iteration.")


# ===========================================================================
# Profiler
# ===========================================================================


class ProfilerResponse(BaseModel):
    """Structured response from the nsys profiler agent."""

    analysis: str = Field(description="Detailed interpretation of the nsys profiling data — what the kernel breakdown, CPU overhead, and GPU idle gaps reveal about the implementation.")
    bottlenecks: str = Field(description="Top bottlenecks identified, ordered by impact. Each bottleneck should name the specific kernel or operation and its contribution to total time.")
    suggestions: str = Field(description="Actionable optimization suggestions for the implementer, tied to specific bottlenecks. E.g. 'Fuse the 12 RMSNorm kernel launches into a single FlashInfer call' or 'Enable CUDA graphs to eliminate 6ms of CPU launch overhead'.")


class ProfilerSummary(BaseModel):
    """Structured summary from the profiler agent, shared with the orchestrator.

    Extends the core profiler response with an optional numeric ``perf_metric``
    the framework uses for regression detection across rounds.
    """

    analysis: str = Field(description="Detailed interpretation of the profile data.")
    bottlenecks: str = Field(description="Ranked bottlenecks with concrete numbers.")
    suggestions: str = Field(description="Actionable optimization suggestions tied to bottlenecks.")
    perf_metric: float | None = Field(
        default=None,
        description="Primary performance metric collected during profiling (higher is better). None when unavailable.",
    )
    perf_unit: str | None = Field(
        default=None,
        description="Unit of perf_metric (e.g. 'req/s', 'tok/s'). None when perf_metric is None.",
    )
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Optional multi-metric dict keyed by metric name (e.g. "
            "{'median_tok_per_sec': 42.1, 'p99_latency_ms': 87.3}). Used by "
            "the evolve loop's Pareto-frontier selection. Single-objective "
            "consumers (agent-loop plateau detection) ignore this field; "
            "they read perf_metric instead."
        ),
    )


# ===========================================================================
# Plain loop (issue-board) variants
# ===========================================================================


class IssueImplementerResponse(BaseModel):
    """Structured response from the implementer agent in the plain loop.

    The implementer works on exactly one issue per invocation.
    """

    issue_id: int = Field(description="ID of the issue this implementer worked on.")
    summary: str = Field(description="What was implemented or changed for this specific issue.")
    files_touched: list[str] = Field(default_factory=list, description="List of files created or modified.")
    self_check: str = Field(description="Brief note on how the implementer self-validated the change before declaring done.")


class IssueJudgeResponse(BaseModel):
    """Structured response from the judge agent in the plain loop.

    The judge evaluates whether the current issue was sufficiently
    resolved while also retaining the basic correctness/test checks of
    the cross-loop judge.
    """

    issue_id: int = Field(description="ID of the issue under review.")
    analysis: str = Field(description="Detailed analysis covering whether the issue is resolved AND general correctness checks.")
    feedback: str = Field(description="Specific actionable feedback for the implementer if not resolved. Empty if PASS.")
    verdict: Verdict = Field(description="PASS if the issue is resolved AND general checks pass, FAIL otherwise.")
    new_issues_filed: list[int] = Field(default_factory=list, description="IDs of new bug-type issues the judge filed via create_issue for unrelated discoveries.")


class IssuePerfEvalResponse(BaseModel):
    """Structured response from the performance evaluator in the plain loop.

    Optimization ideas are filed as issues via the create_issue tool, NOT
    returned in this payload (cf. ``PerfEvalResponse.implementer_feedback``).
    """

    analysis: str = Field(description="What the evaluator observed — trends, bottlenecks, saturation points.")
    metrics: PerfMetrics = Field(description="Structured performance data collected from benchmark runs.")
    evaluator_feedback: list[str] = Field(description="Bullet-point list of notes for the next performance evaluator (e.g. benchmarking strategy, load levels to try, metrics to watch).")
    new_issue_ids: list[int] = Field(default_factory=list, description="IDs of issues filed via create_issue this round.")
    throughput_trend: PerfTrend = Field(description="Whether throughput (req/s, tok/s) improved, regressed, or is mixed compared to the previous iteration.")
    latency_trend: PerfTrend = Field(description="Whether latency (TTFT, TPOT, total) improved, regressed, or is mixed compared to the previous iteration.")


# ===========================================================================
# Agent loop (orchestrator-driven)
# ===========================================================================


class PreRoundDecision(BaseModel):
    """Pre-round decision: does the orchestrator need a profile before planning?"""

    need_profile: bool = Field(
        description="True if a profiler run should happen before the orchestrator plans this round's task."
    )
    profile_focus: str = Field(
        default="",
        description="Guidance for the profiler (e.g. 'focus on decode-path kernels'). Empty when need_profile is False.",
    )
    reasoning: str = Field(
        description="Short explanation of the decision. One or two sentences."
    )


class OrchestratorPlan(BaseModel):
    """The per-round plan produced by the orchestrator.

    The framework applies the plan in this order: optional
    ``revert_to_round`` git checkout, then implementer with ``task``,
    then judge with ``pass_criteria``. The loop always runs the full
    ``max_rounds`` budget; there is no early-stop signal from the
    orchestrator.
    """

    task: str = Field(
        description="Well-scoped task description handed to the implementer."
    )
    pass_criteria: str = Field(
        description="Feature-level pass criteria for the judge. The framework always runs the accuracy checker and benchmark sanity in addition."
    )
    revert_to_round: int | None = Field(
        default=None,
        description="Optional round number to roll back the workspace to (via git checkout of that round's commit) before the implementer runs.",
    )
    reasoning: str = Field(
        description="Short explanation of the orchestrator's reasoning for this round."
    )


# ===========================================================================
# Evolve loop (mutator)
# ===========================================================================


class MutatorResponse(BaseModel):
    """Structured response from the Mutator agent.

    The Mutator is the LLM-as-mutation-operator: given a parent program
    + inspiration peers, it edits the workspace files in place and
    returns a short rationale. The rationale is recorded on the
    offspring's ``Individual`` so future rounds can read it as part of
    population history.
    """

    summary: str = Field(
        description="Short description of the change made to the parent."
    )
    hypothesis: str = Field(
        description="Why this change is expected to improve the headline metric.",
    )
    expected_behavior: str = Field(
        description="Observable change a reviewer should expect (e.g. 'CUDA graph replays > 0', 'tok/s improves vs parent')."
    )
