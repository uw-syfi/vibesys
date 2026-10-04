"""Typed prompt inputs and agent replies owned by the multi policy."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.hypothesis import (
    ArchiveConflict,
    CandidateDisposition,
    HypothesisOutcome,
    SkillResourceSelection,
)
from vibesys.orchestration.progress import ProgressEntry
from vibesys.orchestration.review import Verdict
from vibesys.profile_focus import FocusLedger
from vs_runtime.api import (
    VALIDATION_RECIPE_ARTIFACT_DESCRIPTION,
    ResolvedSkillResources,
    ValidationRecipeArtifactPath,
)


class PlanContext(BaseModel):
    """Changing evidence rendered for one independent designer turn."""

    model_config = ConfigDict(frozen=True)

    objective_location: str
    profiler_entry: ProgressEntry | None
    regression_entry: ProgressEntry | None
    exhaustion_entry: ProgressEntry | None
    progress_location: str
    roadmap_location: str
    pareto_archive_location: str
    plateau_warning: str | None
    domain_orchestrator: str
    runtime_notes: str
    framework_benchmark_enabled: bool
    official_eval_every: int
    provisional_candidates: int
    official_eval_cadence_due: bool
    active_component: str | None = None
    ledger: FocusLedger | None = None
    ranked_bottlenecks: list[dict[str, object]] = Field(default_factory=list)


class ImplementerContext(BaseModel):
    """Changing evidence rendered for an initial implementation turn."""

    model_config = ConfigDict(frozen=True)

    reference_path: str
    modality: str | None
    interface: str
    domain_implementer: str
    objective_location: str
    plan_artifact_location: str
    progress_location: str
    pareto_archive_location: str
    validation_location: str
    validation_recipe_contract_location: str
    retry: int
    feedback: str | None
    framework_revert_applied: bool
    framework_revert_round: int | None
    framework_revert_commit: str | None
    gate_revalidation_pending: bool
    gate_approved_perf_metric: float | None
    gate_approved_perf_unit: str | None
    gate_approved_evaluation_artifact: str | None
    runtime_notes: str
    framework_benchmark_enabled: bool
    official_evaluation_due: bool
    official_evaluation_reason: str | None
    recommended_skills: list[ResolvedSkillResources]
    prior_attempt_artifact_locations: tuple[str, ...]
    active_component: str | None = None


class ImplementerContinuationContext(BaseModel):
    """Changing evidence rendered for a bounded continuation turn."""

    model_config = ConfigDict(frozen=True)

    hypothesis_id: str
    objective_location: str
    plan_artifact_location: str
    progress_location: str
    pareto_archive_location: str
    validation_location: str
    validation_recipe_contract_location: str
    runtime_notes: str
    prior_attempt_artifact_locations: tuple[str, ...]
    retry: int
    continuation_step: str
    feedback: str | None
    recommended_skills: list[ResolvedSkillResources]
    framework_revert_applied: bool
    framework_revert_round: int | None
    framework_revert_commit: str | None
    gate_revalidation_pending: bool
    gate_approved_evaluation_artifact: str | None
    current_round_location: str | None = None


class ImplementerResponse(BaseModel):
    """Structured response from a multi-policy implementer."""

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
        description="Concrete remaining action for a nonterminal or failed outcome.",
    )
    perf_metric: FiniteFloat | None = Field(
        default=None,
        description="Fresh canonical headline metric, otherwise None.",
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
            "Independent checkpoint retention: pareto_frontier, prerequisite, discard, or unassessed."
        ),
    )
    candidate_metrics: dict[str, FiniteFloat] = Field(
        default_factory=dict,
        description="Objective values from one fresh comparable provisional row.",
    )
    candidate_evaluation_artifact: str | None = Field(
        default=None,
        description="Workspace-relative raw artifact for candidate_metrics.",
    )
    candidate_operating_point: str = Field(
        default="",
        description="Workload/load/configuration identity for candidate_metrics.",
    )
    candidate_retention_reason: str = Field(
        default="",
        description="Reason for the checkpoint retention recommendation.",
    )
    skill_context_updates: list[SkillResourceSelection] = Field(
        default_factory=list,
        description="New advisory skill resources consulted or selected this turn.",
    )
    validation_recipe_artifact: ValidationRecipeArtifactPath | None = Field(
        default=None,
        description=VALIDATION_RECIPE_ARTIFACT_DESCRIPTION,
    )


class JudgeContext(BaseModel):
    """Changing evidence rendered for an independent judge turn."""

    model_config = ConfigDict(frozen=True)

    domain_judge: str
    framework_benchmark_enabled: bool
    framework_revert_applied: bool
    framework_revert_round: int | None
    framework_revert_commit: str | None
    gate_approved_evaluation_artifact: str | None
    gate_approved_perf_metric: float | None
    gate_approved_perf_unit: str | None
    gate_revalidation_pending: bool
    implementer_artifact_location: str
    interface: str
    modality: str | None
    objective_location: str
    official_evaluation_due: bool
    official_evaluation_reason: str | None
    pareto_archive_conflict: ArchiveConflict | None
    pareto_archive_location: str
    plan_artifact_location: str
    progress_location: str
    retry: int
    runtime_notes: str
    validation_location: str
    validation_recipe_contract_location: str


class JudgeResponse(BaseModel):
    """Structured response from a multi-policy judge."""

    analysis: str = Field(
        description=(
            "Detailed analysis covering correctness, completeness, dependencies, "
            "tests, and code quality."
        )
    )
    feedback: str = Field(description="Specific actionable feedback. Empty string if passing.")
    verdict: Verdict = Field(
        description=(
            "PASS when verified evidence supports the declared outcome and checkpoint "
            "disposition under the objective invariants; final success criteria need not "
            "hold for a justified continue, disproven, or blocked outcome. FAIL otherwise."
        )
    )
    skills_used: list[SkillResourceSelection] = Field(
        default_factory=list,
        description=(
            "Skill resources independently selected for this review; observational "
            "only and never inherited by the implementer."
        ),
    )


class PreRoundContext(BaseModel):
    """Changing evidence for the pre-round profiling decision."""

    model_config = ConfigDict(frozen=True)

    objective_location: str
    regression_entry: ProgressEntry | None
    exhaustion_entry: ProgressEntry | None
    progress_location: str
    profiler_kind: str
    profile_execution: str
    has_history: bool


class PreRoundDecision(BaseModel):
    """Whether a specialist profile should precede planning."""

    need_profile: bool = Field(
        description="True when a profiler run should happen before planning."
    )
    profile_focus: str = Field(
        default="",
        description="Profiler guidance. Empty when need_profile is False.",
    )
    reasoning: str = Field(description="Short explanation of the decision.")


class ProfilerCampaign(BaseModel):
    """Where a profiler reads campaign progress and writes its evidence."""

    model_config = ConfigDict(frozen=True)

    progress_location: str
    evidence_location: str


class ProfilerContext(BaseModel):
    """Changing evidence rendered for a selected profiler kind."""

    model_config = ConfigDict(frozen=True)

    profile_focus: str
    benchmark_command: str | None
    modality: str | None
    domain_profiler: str
    runtime_notes: str
    profile_execution: str
    objective: str | None
    profiler_support_name: str
    profiler_mcp_name: str
    campaign: ProfilerCampaign | None


__all__ = [
    "ImplementerContext",
    "ImplementerContinuationContext",
    "ImplementerResponse",
    "JudgeContext",
    "JudgeResponse",
    "PlanContext",
    "PreRoundContext",
    "PreRoundDecision",
    "ProfilerCampaign",
    "ProfilerContext",
]
