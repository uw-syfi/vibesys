"""Prompt rendering owned by the evolve plugin."""

from pathlib import Path

from vibesys.orchestration.evolve.models import (
    CandidateJudgeContext,
    CandidateProfilerContext,
    MutatorContext,
)
from vibesys.prompts import PROMPTS_DIR
from vs_prompts.api import TemplateRenderer

_PROMPT_DIR = Path(__file__).resolve().parent
_RENDERER = TemplateRenderer(
    _PROMPT_DIR,
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
