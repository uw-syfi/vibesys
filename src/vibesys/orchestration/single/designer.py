"""Designer planning policy for the real single-agent orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.hypothesis import (
    InvalidPlanError,
    OrchestratorPlan,
    SkillResourceSelection,
    normalize_hypothesis_title,
)
from vibesys.orchestration.prompts import render_plan_correction
from vibesys.orchestration.single.agents import DESIGNER
from vibesys.orchestration.single.prompts import render_plan_prompt
from vibesys.orchestration.structured_turn import structured_turn
from vs_runtime.api import Run, SkillCatalogError, SkillResourceRequest

if TYPE_CHECKING:
    from vibesys.orchestration.hypothesis import HypothesisSearch, HypothesisState
    from vibesys.orchestration.single.models import PlanContext
    from vs_runtime.api import Workspace


@dataclass(frozen=True, slots=True)
class DesignerPlanRequest:
    """All changing evidence needed for one new-hypothesis planning turn."""

    round_number: int
    state: HypothesisState
    context: PlanContext
    workspace: Workspace

    def __post_init__(self) -> None:
        """Reject a round that cannot name its fallback hypothesis."""
        if self.round_number < 1:
            message = "designer round_number must be positive"
            raise ValueError(message)


def _validate_plan(
    plan: OrchestratorPlan, state: HypothesisState, search: HypothesisSearch
) -> None:
    updates = [item.hypothesis_id for item in plan.hypothesis_updates]
    if len(updates) != len(set(updates)):
        raise InvalidPlanError.duplicate_updates()
    if plan.hypothesis_id in updates:
        raise InvalidPlanError.self_reference()
    if state.by_id(plan.hypothesis_id) is not None:
        raise InvalidPlanError.reused_id(plan.hypothesis_id)
    search.validate_updates(state, plan.hypothesis_updates)


def _correction_message(plan: OrchestratorPlan, error: ValueError) -> str:
    return render_plan_correction(
        error=str(error),
        hypothesis_id=plan.hypothesis_id,
        updated_hypothesis_ids=[item.hypothesis_id for item in plan.hypothesis_updates],
        require_unseen_id=True,
    )


async def _resolve_recommendations(run: Run, plan: OrchestratorPlan) -> None:
    if not plan.recommended_skills:
        return
    requests = tuple(
        SkillResourceRequest(
            name=item.skill,
            resource_paths=tuple(item.resource_paths),
            purpose=item.purpose,
        )
        for item in plan.recommended_skills
    )
    try:
        result = await run.skills.resolve(requests)
    except SkillCatalogError as error:
        run.observations.warning(
            f"[skills] ignored recommendations because the catalog is invalid: {error}"
        )
        plan.recommended_skills = []
        return
    for diagnostic in result.diagnostics:
        run.observations.warning(f"[skills] {diagnostic}")
    plan.recommended_skills = [
        SkillResourceSelection(
            skill=item.name,
            resource_paths=[path.removeprefix(f"{item.name}/") for path in item.resource_paths],
            purpose=item.purpose,
        )
        for item in result.resolved
    ]


async def request_plan(
    run: Run, search: HypothesisSearch, request: DesignerPlanRequest
) -> OrchestratorPlan:
    """Return one normalized, state-valid plan after at most one correction.

    A single run-owned designer session carries the rejected plan into its
    correction turn. Resource cleanup occurs on success, failure, and cancellation.
    """
    session = await run.agents.create_session(
        DESIGNER,
        workspace=request.workspace,
        writable_paths=(request.context.roadmap_location,),
    )
    try:
        plan = await structured_turn(session, render_plan_prompt(request.context), OrchestratorPlan)
        for attempt in range(2):
            plan.hypothesis_id = (
                plan.hypothesis_id.strip() or f"hypothesis-{request.round_number:04d}"
            )
            plan.title = normalize_hypothesis_title(plan.title)
            try:
                _validate_plan(plan, request.state, search)
            except ValueError as error:
                if attempt:
                    raise
                run.observations.warning(
                    f"[orchestrator] plan rejected ({error}); reprompting once"
                )
                plan = await structured_turn(
                    session, _correction_message(plan, error), OrchestratorPlan
                )
                continue
            await _resolve_recommendations(run, plan)
            return plan
        message = "designer correction loop exited without a validated plan"
        raise RuntimeError(message)
    finally:
        await session.close()


__all__ = ["DesignerPlanRequest", "request_plan"]
