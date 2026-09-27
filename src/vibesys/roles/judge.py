"""Judge role family: hypothesis and candidate judge roles.

Covers ``multi``'s hypothesis judge and ``evolve``'s candidate judge.

``CANDIDATE_JUDGE`` (evolve's offspring judge) reuses ``JudgeResponse`` but
renders its own template with its own context model, so it stays a distinct
``Role`` in this module rather than a variant of ``MULTI_JUDGE``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vibesys.roles.common import SkillResourceSelection, Verdict
from vibesys.runtime import Fresh, ReadOnly, Reuse, Role


class JudgeContext(BaseModel):
    """Context for multi's hypothesis-judge role.

    Like ``ImplementerContext``, the judge reads the implementer's reported
    evidence from ``implementer_artifact_location`` via tools; only the
    fields the template actually references belong here (not the raw
    ``ImplementerResponse``/plan fields turns.py has on hand).
    """

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
    pareto_archive_conflict: str | None
    pareto_archive_location: str
    plan_artifact_location: str
    progress_location: str
    retry: int
    runtime_notes: str
    validation_location: str
    validation_recipe_contract_location: str


class CandidateJudgeContext(BaseModel):
    """Context for evolve's candidate-judge role (``judge_prompt.j2``)."""

    model_config = ConfigDict(frozen=True)

    accuracy_command: str | None
    benchmark_command: str | None
    domain_judge: str
    interface: str
    modality: str | None
    objective: str | None
    pass_criteria: str
    runtime_notes: str


class JudgeResponse(BaseModel):
    """Structured response from the hypothesis judge agent (multi)."""

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


def _fallback_hypothesis_judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="Judge produced no structured response.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
    )


def _fallback_candidate_judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="Judge produced no structured response.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
    )


MULTI_JUDGE = Role(
    id="judge",
    template="loops/multi/judge_prompt.j2",
    reply=JudgeResponse,
    fallback=_fallback_hypothesis_judge,
    context=JudgeContext,
    access=ReadOnly(),
    session=Fresh(),
    filter_skills=True,
    message="Review the implementation per the criteria above. Return only the JSON verdict.",
)

CANDIDATE_JUDGE = Role(
    id="judge",
    template="loops/evolve/judge_prompt.j2",
    reply=JudgeResponse,
    fallback=_fallback_candidate_judge,
    context=CandidateJudgeContext,
    access=ReadOnly(),
    session=Reuse(),
    message="Review the offspring per the criteria above. Return only the JSON verdict.",
)

ALL_ROLES = (MULTI_JUDGE, CANDIDATE_JUDGE)
