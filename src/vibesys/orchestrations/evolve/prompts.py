"""Prompt rendering owned by the evolve plugin."""

from vibesys.prompts import PROMPTS_DIR
from vibesys.roles.judge import CandidateJudgeContext
from vibesys.roles.mutator import MutatorContext
from vibesys.roles.profiler import CandidateProfilerContext
from vs_prompts.api import TemplateRenderer

_RENDERER = TemplateRenderer(
    PROMPTS_DIR / "loops" / "evolve",
    fallback_roots=(PROMPTS_DIR / "shared", PROMPTS_DIR),
)


def render_mutator(context: MutatorContext) -> str:
    """Render one candidate mutation brief."""
    return _RENDERER.render_template("mutator_prompt.j2", **context.model_dump())


def render_judge(context: CandidateJudgeContext) -> str:
    """Render one independent candidate review brief."""
    return _RENDERER.render_template("judge_prompt.j2", **context.model_dump())


def render_profiler(template: str, context: CandidateProfilerContext) -> str:
    """Render one profiler-kind candidate brief."""
    return _RENDERER.render_template(f"profilers/{template}.j2", **context.model_dump())


__all__ = ["render_judge", "render_mutator", "render_profiler"]
