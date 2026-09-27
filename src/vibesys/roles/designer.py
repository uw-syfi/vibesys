"""Plan-role family for multi's round-planning designer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.runtime import Fresh, ReadOnly, Role
from vibesys.search.hypothesis import OrchestratorPlan


class PlanContext(BaseModel):
    """Context for multi's designer/plan role."""

    model_config = ConfigDict(frozen=True)

    objective_location: str
    profiler_summary: ProfilerSummary | None
    regression_info: str | None
    exhaustion_info: str | None
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
    ledger_text: str | None = None
    ranked_bottlenecks: list[dict[str, object]] = Field(default_factory=list)


def _fallback_plan() -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "task": "Re-check minimal server boots and /health returns 200.",
            "pass_criteria": "/health returns 200.",
            "reasoning": "fallback: orchestrator produced no structured response",
        }
    )


MULTI_ORCHESTRATOR_PLAN = Role(
    id="orchestrator",
    template="loops/multi/orchestrator_plan_prompt.j2",
    reply=OrchestratorPlan,
    fallback=_fallback_plan,
    context=PlanContext,
    access=ReadOnly(),  # allow-list (roadmap index) resolved per call by the caller
    session=Fresh(),
    filter_skills=True,
    message="Produce this round's plan. Return only the JSON object.",
)

ALL_ROLES = (MULTI_ORCHESTRATOR_PLAN,)
