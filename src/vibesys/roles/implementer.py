"""Implementer role family for multi's hypothesis implementers.

``single`` and ``profile_single`` fold implementer + judge + profiler into
one plugin-owned agent role.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.roles.common import SkillResourceSelection
from vibesys.runtime import Keyed, Role, Writes
from vibesys.skills import ResolvedSkillSelection
from vs_agent.api import SessionScope
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome


class ImplementerContext(BaseModel):
    """Context for multi's main-task implementer role.

    The active plan's own fields (task, hypothesis, activation evidence, ...)
    are not free variables of ``implementer_prompt.j2``: the implementer reads
    them from ``plan_artifact_location`` via tools, not from the prompt text.
    """

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
    recommended_skills: list[ResolvedSkillSelection]
    prior_attempt_artifact_locations: tuple[str, ...]
    active_component: str | None = None


class ImplementerContinuationContext(BaseModel):
    """Context for multi's continuation-step implementer role."""

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
    recommended_skills: list[ResolvedSkillSelection]
    framework_revert_applied: bool
    framework_revert_round: int | None
    framework_revert_commit: str | None
    gate_revalidation_pending: bool
    gate_approved_evaluation_artifact: str | None
    current_round_location: str | None = None


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


def _fallback_implementer() -> ImplementerResponse:
    return ImplementerResponse(
        summary="Implementer produced no structured response.",
        expected_behavior="unknown",
        hypothesis_outcome="inconclusive",
        evidence="The implementer output could not be parsed.",
        next_step="Recover retained evidence and return a schema-valid response before review.",
    )


def _timeout_fallback_implementer(timeout: float) -> ImplementerResponse:
    return ImplementerResponse(
        summary="Implementer invocation timed out.",
        expected_behavior="unknown",
        hypothesis_outcome="inconclusive",
        evidence=(
            f"The framework stopped the implementer after {timeout:g} seconds "
            "without a structured response."
        ),
        next_step="Inspect retained evidence and return a schema-valid response on retry.",
    )


MULTI_IMPLEMENTER = Role(
    id="implementer",
    template="loops/multi/implementer_prompt.j2",
    reply=ImplementerResponse,
    fallback=_fallback_implementer,
    context=ImplementerContext,
    timeout_fallback=_timeout_fallback_implementer,
    access=Writes(),
    session=Keyed(scope=SessionScope.HYPOTHESIS),
    paid=True,
    filter_skills=True,
    message="Work persistently on the active hypothesis and return only the JSON object.",
)

MULTI_IMPLEMENTER_CONTINUATION = Role(
    id="implementer",
    template="loops/multi/implementer_continuation_prompt.j2",
    reply=ImplementerResponse,
    fallback=_fallback_implementer,
    context=ImplementerContinuationContext,
    timeout_fallback=_timeout_fallback_implementer,
    access=Writes(),
    session=Keyed(scope=SessionScope.HYPOTHESIS),
    paid=True,
    filter_skills=True,
    message="Execute the required continuation step and return only the JSON object.",
)

ALL_ROLES = (MULTI_IMPLEMENTER, MULTI_IMPLEMENTER_CONTINUATION)
