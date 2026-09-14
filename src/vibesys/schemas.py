"""Pydantic schemas for structured agent responses.

Every Pydantic model the framework uses to constrain an LLM's JSON
output lives here, organized by purpose:

  - Enums:                Verdict, PerfTrend
  - Skill routing:        SkillResourceSelection
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

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator

# ===========================================================================
# Enums
# ===========================================================================


class Verdict(StrEnum):  # noqa: D101  # tracked: #288
    PASS = "pass"  # noqa: S105  # tracked: #288
    FAIL = "fail"


class HypothesisOutcome(StrEnum):
    """Implementer-owned status for the active experimental hypothesis.

    ``SUPPORTED`` and ``NOMINATED`` are deliberately distinct from
    ``PROVEN``: an implementer may submit evidence for independent review,
    but only the judge can establish that the scoped hypothesis held.
    ``NOMINATED`` additionally asks the framework to run its global gates for
    the current candidate checkpoint. It does not imply that the overall
    objective or terminal target has been achieved.
    """

    CONTINUE = "continue"
    SUPPORTED = "supported"
    NOMINATED = "nominated"
    DISPROVEN = "disproven"
    IMPLEMENTATION_FAILED = "implementation_failed"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"


class CandidateDisposition(StrEnum):
    """How a measured candidate should be retained independently of its hypothesis.

    Hypothesis truth and checkpoint utility are different questions. A causal
    forecast can be disproven while its implementation still establishes a
    useful throughput/latency tradeoff. These values keep that distinction
    explicit without promoting provisional evidence to an official result.
    """

    UNASSESSED = "unassessed"
    DISCARD = "discard"
    PREREQUISITE = "prerequisite"
    PARETO_FRONTIER = "pareto_frontier"


class PerfDeltaReason(StrEnum):
    """Why a headline measurement carries no causal delta.

    Always re-derived from round evidence (``perf_provenance`` and the
    baseline fields), never stored on the round record, so it cannot drift
    from them. Absent entirely for records that predate provenance tracking:
    a legacy absolute number keeps reading as a deliberate absolute rather
    than being relabelled as unresolved.
    """

    # No trusted official measurement of the metric existed yet, so there was
    # legitimately nothing to compare against.
    NO_BASELINE_YET = "no_baseline_yet"
    # Trusted measurements existed but none was admissible as this round's
    # causal baseline: the lookup failed closed rather than inverting cause
    # and effect.
    BASELINE_UNRESOLVED = "baseline_unresolved"
    # The only headline number is the implementer's own report, which the
    # framework never orders against anything.
    NOT_FRAMEWORK_MEASURED = "not_framework_measured"


HypothesisStrategyDisposition = Literal["parked", "abandoned"]


class HypothesisStrategyUpdate(BaseModel):
    """One structured update to a previously completed hypothesis."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1)
    disposition: HypothesisStrategyDisposition
    reason: str = Field(min_length=1)

    @field_validator("hypothesis_id", "reason")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        if not (stripped := value.strip()):
            raise ValueError("must not be blank")  # noqa: TRY003  # tracked: #288
        return stripped


class PerfTrend(StrEnum):  # noqa: D101  # tracked: #288
    IMPROVED = "improved"
    REGRESSED = "regressed"
    MIXED = "mixed"


class SkillResourceSelection(BaseModel):
    """Advisory selection of resources from one installed agent skill.

    The outer loop can recommend these resources, while implementation and
    review agents remain free to select different installed skills.  Paths are
    relative to the named skill root and are resolved by the framework before
    they are shown to another agent.
    """

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1, description="Exact installed skill name.")
    resource_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Optional files relative to the skill root. An empty list selects "
            "the skill router without preselecting a resource."
        ),
    )
    purpose: str = Field(
        min_length=1,
        description="Short reason these resources may help with the current work.",
    )

    @field_validator("skill", "purpose")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must contain non-whitespace text")  # noqa: TRY003  # tracked: #288
        return value


# ===========================================================================
# Implementer / Judge — cross-loop
# ===========================================================================


class ValidationRecipe(BaseModel):
    """A bounded, reusable local validation command proposed by an implementer.

    The independent judge audits the recipe before the framework executes it.
    Target, deployment, benchmark, profiler, and official evaluator commands
    belong to their existing framework-owned gates instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        description="Stable short identifier for this validation recipe.",
    )
    command: str = Field(
        min_length=1,
        max_length=4000,
        description="Exact non-interactive command to execute from the workspace root.",
    )
    input_paths: list[str] = Field(
        min_length=1,
        max_length=64,
        description=(
            "Workspace-relative source, test, lock, or configuration paths that "
            "fully determine whether a prior passing result can be reused."
        ),
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=1800,
        description="Hard wall-clock timeout for this local validation command.",
    )
    purpose: str = Field(
        min_length=1,
        max_length=500,
        description="The observable contract this command validates.",
    )

    @field_validator("command", "purpose")
    @classmethod
    def _strip_recipe_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must contain non-whitespace text")  # noqa: TRY003  # tracked: #288
        return value

    @field_validator("input_paths")
    @classmethod
    def _validate_input_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip()
            path = PurePosixPath(value)
            if not value or path.is_absolute() or value == "." or ".." in path.parts:
                raise ValueError(  # noqa: TRY003  # tracked: #288
                    "input_paths must contain non-empty workspace-relative paths "
                    "without parent traversal"
                )
            normalized.append(path.as_posix())
        if len(set(normalized)) != len(normalized):
            raise ValueError("input_paths must not contain duplicates")  # noqa: TRY003  # tracked: #288
        return normalized


class ValidationRecipeArtifact(BaseModel):
    """Versioned candidate-authored container for local validation recipes."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "focused-tests",
                            "command": "uv run pytest -q test_server.py",
                            "input_paths": [
                                "server.py",
                                "test_server.py",
                                "pyproject.toml",
                                "uv.lock",
                            ],
                            "timeout_seconds": 300,
                            "purpose": "Exercise the focused local server contract.",
                        }
                    ],
                }
            ]
        },
    )

    version: Literal[1] = 1
    recipes: list[ValidationRecipe] = Field(min_length=1, max_length=8)


class FrameworkValidationResult(BaseModel):
    """Framework-owned result for one audited validation recipe."""

    model_config = ConfigDict(extra="forbid")

    recipe: ValidationRecipe
    input_digest: str
    passed: bool
    reused: bool = False
    exit_code: int | None = None
    output: str = ""
    error: str | None = None


class ImplementerResponse(BaseModel):
    """Structured response from the implementer agent."""

    summary: str = Field(description="What was implemented or changed this iteration.")
    expected_behavior: str = Field(
        description="Observable behavior expected from the implementation."
    )
    hypothesis_outcome: HypothesisOutcome = Field(
        default=HypothesisOutcome.NOMINATED,
        description=(
            "Hypothesis lifecycle status. Use continue only for bounded unfinished "
            "same-mechanism work; supported/nominated require review readiness."
        ),
    )
    evidence: str = Field(
        default="",
        description="Observed evidence supporting the reported hypothesis outcome.",
    )
    next_step: str = Field(
        default="",
        description=("Concrete remaining action for a nonterminal or failed outcome."),
    )
    perf_metric: FiniteFloat | None = Field(
        default=None,
        description=("Fresh canonical headline metric, otherwise None."),
    )
    perf_unit: str | None = Field(
        default=None,
        description="Unit of perf_metric, otherwise None.",
    )
    metrics: dict[str, FiniteFloat] = Field(
        default_factory=dict,
        description="Objective metrics from the same canonical row.",
    )
    evaluation_artifact: str | None = Field(
        default=None,
        description="Workspace-relative canonical evaluation artifact.",
    )
    candidate_disposition: CandidateDisposition = Field(
        default=CandidateDisposition.UNASSESSED,
        description=(
            "Independent checkpoint retention: frontier, prerequisite, discard, or unassessed."
        ),
    )
    candidate_metrics: dict[str, FiniteFloat] = Field(
        default_factory=dict,
        description=("Objective values from one fresh comparable provisional row."),
    )
    candidate_evaluation_artifact: str | None = Field(
        default=None,
        description=("Workspace-relative raw artifact for candidate_metrics."),
    )
    candidate_operating_point: str = Field(
        default="",
        description=("Workload/load/configuration identity for candidate_metrics."),
    )
    candidate_retention_reason: str = Field(
        default="",
        description=("Reason for the checkpoint retention recommendation."),
    )
    skill_context_updates: list[SkillResourceSelection] = Field(
        default_factory=list,
        description=("New advisory skill resources consulted or selected this turn."),
    )
    validation_recipe_artifact: str | None = Field(
        default=None,
        description=("Workspace-relative framework local-validation recipe JSON."),
    )


class JudgeResponse(BaseModel):
    """Structured response from the judge agent."""

    analysis: str = Field(
        description="Detailed analysis of the implementation covering correctness, completeness, dependencies, tests, and code quality."
    )
    feedback: str = Field(
        description="Specific actionable feedback for the implementer. Empty string if passing."
    )
    verdict: Verdict = Field(description="PASS if all criteria are met, FAIL otherwise.")
    skills_used: list[SkillResourceSelection] = Field(
        default_factory=list,
        description=(
            "Skill resources independently selected for this review; observational "
            "only and never inherited by the implementer."
        ),
    )


# ===========================================================================
# Performance metrics + perf-eval response (cross-loop)
# ===========================================================================


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


# ===========================================================================
# Profiler
# ===========================================================================


class ProfilerResponse(BaseModel):
    """Structured response from a profiler agent."""

    analysis: str = Field(
        description=(
            "Detailed interpretation of collected profiling data, including "
            "visibility limits and observed CPU, accelerator, or runtime evidence."
        )
    )
    bottlenecks: str = Field(
        description=(
            "Measured bottlenecks ordered by impact, or an explicit attribution gap "
            "when the capture cannot observe the production mechanism."
        )
    )
    suggestions: str = Field(
        description=(
            "Advisory optimization or measurement suggestions tied to measured "
            "evidence and estimated end-to-end impact; they do not block planning."
        )
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
        description="Primary performance metric collected during profiling (higher is better). None when unavailable.",
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


# ===========================================================================
# Plain loop (issue-board) variants
# ===========================================================================


class IssueImplementerResponse(BaseModel):
    """Structured response from the implementer agent in the plain loop.

    The implementer works on exactly one issue per invocation.
    """

    issue_id: int = Field(description="ID of the issue this implementer worked on.")
    summary: str = Field(description="What was implemented or changed for this specific issue.")
    files_touched: list[str] = Field(
        default_factory=list, description="List of files created or modified."
    )
    self_check: str = Field(
        description="Brief note on how the implementer self-validated the change before declaring done."
    )


class IssueJudgeResponse(BaseModel):
    """Structured response from the judge agent in the plain loop.

    The judge evaluates whether the current issue was sufficiently
    resolved while also retaining the basic correctness/test checks of
    the cross-loop judge.
    """

    issue_id: int = Field(description="ID of the issue under review.")
    analysis: str = Field(
        description="Detailed analysis covering whether the issue is resolved AND general correctness checks."
    )
    feedback: str = Field(
        description="Specific actionable feedback for the implementer if not resolved. Empty if PASS."
    )
    verdict: Verdict = Field(
        description="PASS if the issue is resolved AND general checks pass, FAIL otherwise."
    )
    new_issues_filed: list[int] = Field(
        default_factory=list,
        description="IDs of new bug-type issues the judge filed via create_issue for unrelated discoveries.",
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
    reasoning: str = Field(description="Short explanation of the decision. One or two sentences.")


HYPOTHESIS_TITLE_MAX_LEN = 60


def truncate_hypothesis_title(text: str) -> str:
    """Truncate ``text`` to ``HYPOTHESIS_TITLE_MAX_LEN`` on a word boundary.

    Adds a trailing ellipsis when truncation occurs. ``text`` is assumed to
    already be stripped and whitespace-collapsed; this only shortens it.
    """
    if len(text) <= HYPOTHESIS_TITLE_MAX_LEN:
        return text
    truncated = text[: HYPOTHESIS_TITLE_MAX_LEN - 1]
    boundary = truncated.rfind(" ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return truncated.rstrip() + "…"


def normalize_hypothesis_title(title: str) -> str:
    """Strip, collapse internal whitespace, and truncate an orchestrator title.

    Returns ``""`` when ``title`` carries no text, so callers can tell an
    absent title apart from one that was merely whitespace.
    """
    collapsed = " ".join(title.split())
    return truncate_hypothesis_title(collapsed)


def derive_hypothesis_title(claim: str) -> str | None:
    """Derive a display title from a hypothesis claim.

    Takes the claim's first line, then its first sentence, strips a trailing
    period, and truncates to ``HYPOTHESIS_TITLE_MAX_LEN``. Returns ``None``
    when the claim carries no text, so callers can distinguish "no claim" from
    a claim that happened to produce an empty title.
    """
    stripped = claim.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    first_sentence = first_line.split(". ", 1)[0].rstrip(". ")
    collapsed = " ".join(first_sentence.split())
    if not collapsed:
        return None
    return truncate_hypothesis_title(collapsed)


class OrchestratorPlan(BaseModel):
    """The per-round plan produced by the orchestrator.

    The framework applies the plan in this order: optional
    ``revert_to_round`` git checkout, then implementer with ``task``,
    then judge with ``pass_criteria``. The loop always runs the full
    ``max_rounds`` budget; there is no early-stop signal from the
    orchestrator.
    """

    hypothesis_id: str = Field(
        default="",
        description=(
            "Stable short identifier for this round's new hypothesis. Never reuse "
            "an identifier used earlier in this run."
        ),
    )
    hypothesis_updates: list[HypothesisStrategyUpdate] = Field(
        default_factory=list,
        description=(
            "Strategic parked/abandoned updates for previously completed hypotheses. "
            "These are persisted by the framework; roadmap prose is not authoritative."
        ),
    )
    hypothesis: str = Field(
        default="",
        description="Causal, falsifiable claim explaining why the proposed change should help.",
    )
    title: str = Field(
        default="",
        description=(
            f"Short plain-language title (<={HYPOTHESIS_TITLE_MAX_LEN} chars, no "
            f"trailing period or 'Hypothesis:' prefix)."
        ),
    )
    activation_evidence: str = Field(
        default="",
        description=(
            "Concise observable evidence that the intended code path or mechanism "
            "ran; include only hypothesis-specific signals."
        ),
    )
    falsification_criteria: str = Field(
        default="",
        description="Evidence that would disprove the causal hypothesis for this workload.",
    )
    expected_effect: str = Field(
        default="",
        description=(
            "Analytical forecast range used to prioritize and later calibrate the "
            "hypothesis; this is not an acceptance threshold."
        ),
    )
    minimum_acceptance_criteria: str = Field(
        default="",
        description=(
            "Minimum observed end-to-end benefit and allowed tradeoffs that justify "
            "retaining the change, derived separately from forecast uncertainty, "
            "benchmark noise, and implementation cost."
        ),
    )
    invariants: str = Field(
        default="",
        description=(
            "Hypothesis-specific invariants and diagnostics. Cite authoritative "
            "objective/runtime paths for stable constraints instead of restating them."
        ),
    )
    recommended_skills: list[SkillResourceSelection] = Field(
        default_factory=list,
        description=(
            "Zero or more installed skill resources that may help implement this "
            "hypothesis. Recommendations are advisory, not an allowlist or gate."
        ),
    )
    task: str = Field(
        description=(
            "Causally complete component/interface and staged-evaluation delta handed "
            "to the implementer, without restating stable authoritative contracts."
        )
    )
    pass_criteria: str = Field(
        description=(
            "Hypothesis-specific activation, correctness, cleanup, and evidence gates. "
            "Reference stable trusted gates by path; they run separately when configured."
        )
    )
    request_official_evaluation: bool = Field(
        default=False,
        description=(
            "Run the framework-owned canonical evaluation after this hypothesis "
            "passes independent review, even when the normal sparse-evaluation "
            "cadence is not yet due."
        ),
    )
    revert_to_round: int | None = Field(
        default=None,
        description=(
            "Optional round number whose candidate tree the framework should "
            "materialize before the implementer runs. The framework preserves "
            "durable experiment memory and does not move Git HEAD."
        ),
    )
    reasoning: str = Field(
        description="Brief decisive comparison supporting this round's parent and mechanism."
    )


# ===========================================================================
# Agent loop — single-agent inner loop (ablation)
# ===========================================================================


class SingleAgentRoundResponse(BaseModel):
    """One agent performs implementer + judge + profiler in a single shot.

    Used by the agent outer-loop's ``--inner-loop=single-agent`` ablation:
    instead of three specialist agents handing off through the framework,
    the same agent implements the round's task, runs the always-on
    correctness checks, and captures a profile, then returns the combined
    verdict.
    """

    summary: str = Field(description="What was implemented or changed this round.")
    expected_behavior: str = Field(
        description="What behavior the implementation should exhibit (server contract, etc.)."
    )
    self_review: str = Field(
        description="Self-review of correctness, accuracy, benchmark sanity, and reward-hack risk — same gates the judge would enforce."
    )
    feedback: str = Field(
        description="Concrete issues to fix on retry; empty when verdict is PASS."
    )
    verdict: Verdict = Field(
        description="PASS if all gates (orchestrator pass criteria + always-on checks) hold; FAIL otherwise."
    )
    bottlenecks: str = Field(description="Ranked profile bottlenecks with concrete numbers.")
    suggestions: str = Field(
        description="Actionable optimization suggestions for the next round, tied to bottlenecks."
    )
    profile_analysis: str = Field(description="Detailed interpretation of the captured profile.")
    perf_metric: FiniteFloat | None = Field(
        default=None,
        description="Headline perf metric from the benchmark (per OBJECTIVE.md). None if not measured.",
    )
    perf_unit: str | None = Field(
        default=None,
        description="Unit/field name for perf_metric (e.g. 'median_tok_per_sec'). None when perf_metric is None.",
    )
    candidate_disposition: CandidateDisposition = Field(
        default=CandidateDisposition.UNASSESSED,
        description="Independent provisional checkpoint-retention recommendation.",
    )
    candidate_metrics: dict[str, FiniteFloat] = Field(
        default_factory=dict,
        description="Objective values from one fresh directly comparable end-to-end row.",
    )
    candidate_evaluation_artifact: str | None = Field(
        default=None,
        description="Workspace-relative raw artifact supporting candidate_metrics.",
    )
    candidate_operating_point: str = Field(
        default="",
        description="Workload/load/configuration identity for candidate_metrics.",
    )
    candidate_retention_reason: str = Field(
        default="",
        description="Reason for retaining or discarding the candidate checkpoint.",
    )
    skill_context_updates: list[SkillResourceSelection] = Field(
        default_factory=list,
        description="New skill resources consulted or selected during this turn.",
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

    summary: str = Field(description="Short description of the change made to the parent.")
    hypothesis: str = Field(
        description="Why this change is expected to improve the headline metric.",
    )
    expected_behavior: str = Field(
        description="Observable change a reviewer should expect (e.g. 'CUDA graph replays > 0', 'tok/s improves vs parent')."
    )
