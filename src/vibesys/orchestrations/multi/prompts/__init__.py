"""Prompt rendering owned by the multi-agent orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.prompts import PROMPTS_DIR
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from vibesys.roles.designer import PlanContext
    from vibesys.roles.implementer import ImplementerContext, ImplementerContinuationContext
    from vibesys.roles.judge import JudgeContext
    from vibesys.roles.pre_round import PreRoundContext
    from vibesys.roles.profiler import ProfilerContext

_PROMPT_DIR = PROMPTS_DIR / "loops" / "multi"
# The shared execution and modality fragments remain common policy assets until
# the final namespace consolidation moves them beside this temporary plugin.
_RENDERER = TemplateRenderer(
    _PROMPT_DIR,
    fallback_roots=(PROMPTS_DIR / "shared", PROMPTS_DIR),
)


def render_pre_round_prompt(context: PreRoundContext) -> str:
    """Render the pre-round profiling decision brief."""
    return _RENDERER.render_template("orchestrator_pre_round_prompt.j2", **context.model_dump())


def render_plan_prompt(context: PlanContext) -> str:
    """Render one new-hypothesis planning brief."""
    return _RENDERER.render_template("orchestrator_plan_prompt.j2", **context.model_dump())


def render_profiler_prompt(template: str, context: ProfilerContext) -> str:
    """Render the selected profiler-kind brief."""
    return _RENDERER.render_template(f"profilers/{template}.j2", **context.model_dump())


def render_implementer_prompt(context: ImplementerContext) -> str:
    """Render the initial implementer brief for one hypothesis."""
    return _RENDERER.render_template("implementer_prompt.j2", **context.model_dump())


def render_continuation_prompt(context: ImplementerContinuationContext) -> str:
    """Render a same-session bounded continuation brief."""
    return _RENDERER.render_template("implementer_continuation_prompt.j2", **context.model_dump())


def render_judge_prompt(context: JudgeContext) -> str:
    """Render the independent evidence-review brief."""
    return _RENDERER.render_template("judge_prompt.j2", **context.model_dump())


__all__ = [
    "render_continuation_prompt",
    "render_implementer_prompt",
    "render_judge_prompt",
    "render_plan_prompt",
    "render_pre_round_prompt",
    "render_profiler_prompt",
]
