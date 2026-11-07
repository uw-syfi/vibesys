"""The replies of the multi-agent roles."""

from typing import Annotated, override

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.hypothesis import (
    CandidateDisposition,
    DecidedFrom,
    HypothesisOutcome,
    ImplementerStatus,
    SkillResourceSelection,
)
from vibesys.orchestration.review import Verdict
from vs_core.api.search import RevisionReply, markdown_prose
from vs_runtime.api import VALIDATION_RECIPE_ARTIFACT_DESCRIPTION, ValidationRecipeArtifactPath


class ImplementerResponse(RevisionReply):
    """Structured response from a multi-policy implementer."""

    summary: str = Field(
        description=markdown_prose("What was implemented or changed this iteration.")
    )
    expected_behavior: str = Field(
        description=markdown_prose("Observable behavior expected from the implementation.")
    )
    status: Annotated[
        ImplementerStatus, DecidedFrom("plan.pass_criteria", "feedback", "validation_failure")
    ] = Field(
        default=ImplementerStatus.IMPLEMENTED,
        description=(
            "Implementation status only. implemented: the candidate is complete and ready for "
            "independent review and the framework's trusted measurement. continue: bounded "
            "unfinished work in the same mechanism. implementation_failed or blocked: "
            "concrete implementation or external conditions prevented a fair test. Whether "
            "the idea held is derived by the framework from its own measurement; never "
            "state it."
        ),
    )
    evidence: str = Field(
        default="",
        description=markdown_prose("Observed evidence supporting the reported hypothesis outcome."),
    )
    next_step: str = Field(
        default="",
        description=markdown_prose(
            "Concrete remaining action for a nonterminal or failed outcome."
        ),
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
    candidate_disposition: Annotated[
        CandidateDisposition, DecidedFrom("plan.minimum_ratio", "pareto_archive_conflict")
    ] = Field(
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
        description=markdown_prose("Reason for the checkpoint retention recommendation."),
    )
    skill_context_updates: list[SkillResourceSelection] = Field(
        default_factory=list,
        description="New advisory skill resources consulted or selected this turn.",
    )
    validation_recipe_artifact: ValidationRecipeArtifactPath | None = Field(
        default=None,
        description=VALIDATION_RECIPE_ARTIFACT_DESCRIPTION,
    )

    @property
    def hypothesis_outcome(self) -> HypothesisOutcome:
        """The ledger's declared outcome for the reported status (`ImplementerStatus.outcome`)."""
        return self.status.outcome

    @override
    def produces_revision(self) -> bool:
        """A blocked implementer claims no work, so core records no checkpoint for its turn."""
        return self.status is not ImplementerStatus.BLOCKED


class JudgeResponse(BaseModel):
    """Structured response from a multi-policy judge."""

    model_config = ConfigDict(extra="forbid")

    analysis: str = Field(
        description=markdown_prose(
            "Detailed analysis covering correctness, completeness, dependencies, "
            "tests, and code quality."
        )
    )
    feedback: str = Field(
        description=markdown_prose("Specific actionable feedback. Empty string if passing.")
    )
    verdict: Annotated[
        Verdict,
        DecidedFrom(
            "plan.pass_criteria",
            "plan.invariants",
            "claims.status",
            "claims.evidence",
            "cited_evidence",
        ),
    ] = Field(
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


class PreRoundDecision(BaseModel):
    """Whether a specialist profile should precede planning."""

    model_config = ConfigDict(extra="forbid")

    need_profile: Annotated[
        bool,
        DecidedFrom(
            "round_number",
            "base_revision",
            "base_value",
            "regression_kind",
            "exhaustion_feedback",
        ),
    ] = Field(description="True when a profiler run should happen before planning.")
    profile_focus: str = Field(
        default="",
        description=markdown_prose("Profiler guidance. Empty when need_profile is False."),
    )
    reasoning: str = Field(description=markdown_prose("Short explanation of the decision."))


__all__ = ["ImplementerResponse", "JudgeResponse", "PreRoundDecision"]
