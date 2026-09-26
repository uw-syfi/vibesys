"""Prompt rendering owned by the single-agent orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.orchestrations.single.models import SingleAgentResult, SingleOptions, SinglePlan
from vibesys.prompts import PROMPTS_DIR
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from vibesys.roles.designer import PlanContext
    from vibesys.roles.single_agent import SingleAgentRoundContext

_PROMPT_DIR = Path(__file__).resolve().parent
# The execution boundary is shared by the legacy policies until they migrate.
_RENDERER = TemplateRenderer(_PROMPT_DIR, fallback_roots=(PROMPTS_DIR / "shared",))


def render_plan_prompt(context: PlanContext) -> str:
    """Render the complete legacy designer brief as changing turn content."""
    return _RENDERER.render_template("orchestrator_plan_prompt.j2", **context.model_dump())


def render_single_agent_prompt(context: SingleAgentRoundContext) -> str:
    """Render the complete legacy implementer brief, including shared execution rules."""
    return _RENDERER.render_template("single_agent_round_prompt.j2", **context.model_dump())


def plan_message(options: SingleOptions) -> str:
    """Render the changing evidence for the designer's first turn."""
    return (
        "Design one optimization plan for this objective:\n\n"
        f"{options.objective}\n\n"
        "Choose a new stable hypothesis_id. State the hypothesis, a concrete task, "
        "measurable pass criteria, and reasoning. Return only the JSON object for "
        f"{SinglePlan.__name__}."
    )


def plan_correction_message(plan: SinglePlan, error: ValueError) -> str:
    """Render the one policy-owned correction after an invalid lifecycle edit."""
    updates = ", ".join(item.hypothesis_id for item in plan.hypothesis_updates) or "(none)"
    return (
        f"Your previous plan was rejected: {error}. It proposed hypothesis_id "
        f"{plan.hypothesis_id!r} and updates [{updates}]. Each prior hypothesis may "
        "appear at most once, and the new hypothesis must not update itself. Produce "
        "one corrected plan. Return only the JSON object."
    )


def implementation_message(plan: SinglePlan) -> str:
    """Render the selected plan for the combined implementer and reviewer."""
    return (
        f"Hypothesis: {plan.hypothesis}\n"
        f"Task: {plan.task}\n"
        f"Pass criteria: {plan.pass_criteria}\n"
        "Implement the task, run relevant checks, self-review the change, and return "
        f"only the JSON object for {SingleAgentResult.__name__}."
    )


def revision_message(result: SingleAgentResult) -> str:
    """Render explicit retry policy while preserving the implementer's context."""
    feedback = result.feedback.strip() or "The self-review did not pass."
    return (
        f"Revise the same implementation. Previous review feedback: {feedback}\n"
        "Re-run the relevant checks and return a complete corrected JSON response."
    )


__all__ = [
    "implementation_message",
    "plan_correction_message",
    "plan_message",
    "render_plan_prompt",
    "render_single_agent_prompt",
    "revision_message",
]
