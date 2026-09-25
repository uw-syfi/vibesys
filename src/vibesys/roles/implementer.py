"""Implementer role family: multi's hypothesis implementer and issue_queue's.

``single`` and ``profile_single`` fold implementer + judge + profiler into
one combined role; see :mod:`vibesys.roles.single_agent` for those.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.roles.common import SkillResourceSelection
from vibesys.runtime import Keyed, Reuse, Role, Writes
from vibesys.skills import ResolvedSkillSelection
from vs_agent.api import SessionScope
from vs_issue_board.api import Issue
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


class IssueImplementerContext(BaseModel):
    """Context for issue_queue's implementer role (its ``system.j2``)."""

    model_config = ConfigDict(frozen=True)

    reference_path: str
    runtime_notes: str
    issue: Issue


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


def _fallback_issue_implementer() -> IssueImplementerResponse:
    """``issue_id=0`` is a placeholder; the caller restores the real ID.

    See the module docstring in :mod:`vibesys.roles.judge` for the same
    per-call correlation-field convention.
    """
    return IssueImplementerResponse(
        issue_id=0,
        summary="Implementer did not produce a structured response.",
        files_touched=[],
        self_check="No structured response received.",
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

ISSUE_IMPLEMENTER = Role(
    id="implementer",
    template="loops/issue_queue/implementer/system.j2",
    reply=IssueImplementerResponse,
    fallback=_fallback_issue_implementer,
    context=IssueImplementerContext,
    access=Writes(),
    session=Reuse(),
)

ALL_ROLES = (MULTI_IMPLEMENTER, MULTI_IMPLEMENTER_CONTINUATION, ISSUE_IMPLEMENTER)
