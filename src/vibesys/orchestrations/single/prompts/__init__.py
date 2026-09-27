"""Prompt rendering owned by the single-agent orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.prompts import PROMPTS_DIR as SHARED_PROMPTS_DIR
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from vibesys.orchestrations.single.models import PlanContext, SingleAgentRoundContext

PROMPT_DIR = Path(__file__).resolve().parent
# The execution boundary is shared by the legacy policies until they migrate.
_RENDERER = TemplateRenderer(PROMPT_DIR, fallback_roots=(SHARED_PROMPTS_DIR / "shared",))


def render_plan_prompt(context: PlanContext) -> str:
    """Render the complete legacy designer brief as changing turn content."""
    return _RENDERER.render_template("orchestrator_plan_prompt.j2", **context.model_dump())


def render_single_agent_prompt(context: SingleAgentRoundContext) -> str:
    """Render the complete legacy implementer brief, including shared execution rules."""
    return _RENDERER.render_template("single_agent_round_prompt.j2", **context.model_dump())


__all__ = ["PROMPT_DIR", "render_plan_prompt", "render_single_agent_prompt"]
