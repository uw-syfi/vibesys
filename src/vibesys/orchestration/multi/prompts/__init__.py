"""Prompt rendering owned by the multi-agent orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.orchestration.prompts import PROMPTS_DIR
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from vibesys.orchestration.hypothesis import (
        ArchiveConflict,
        ExhaustionNotice,
        ParetoArchiveView,
        RegressionNotice,
    )
    from vibesys.orchestration.multi.contracts import (
        ImplementerContext,
        ImplementerContinuationContext,
        JudgeContext,
        PlanContext,
        PreRoundContext,
        ProfilerContext,
    )

PROMPT_DIR = Path(__file__).parent
_RENDERER = TemplateRenderer(
    PROMPT_DIR,
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


def render_archive_conflict(conflict: ArchiveConflict) -> str:
    """Render the live-archive conflict notice the judge's failed verdict carries."""
    return _RENDERER.render_template(
        "_notices/archive_conflict.j2", pareto_archive_conflict=conflict
    )


def render_pareto_guard(review: str, conflict: ArchiveConflict) -> str:
    """Render the judge analysis after the framework's Pareto guard overrode a pass."""
    return _RENDERER.render_template(
        "_notices/pareto_guard.j2", review=review, pareto_archive_conflict=conflict
    )


def render_pareto_frontier(archive: ParetoArchiveView) -> str:
    """Render the derived Pareto archive document agents read from progress."""
    return _RENDERER.render_template("pareto_frontier.j2", archive=archive)


def render_regression_notice(notice: RegressionNotice) -> str:
    """Render the regression or terminal-workspace notice the progress entry carries."""
    return _RENDERER.render_template("_notices/regression.j2", regression_info=notice)


def render_exhaustion_notice(notice: ExhaustionNotice) -> str:
    """Render the exhausted-review feedback the progress entry carries."""
    return _RENDERER.render_template("_notices/exhaustion.j2", exhaustion_info=notice)


def render_turn_failed_feedback(reason: str) -> str:
    """Render the framework feedback for an attempt whose agent returned no valid response."""
    return _RENDERER.render_template("_notices/turn_failed.j2", reason=reason)


def render_system_prompt(role: str) -> str:
    """Render the fixed system prompt of one agent role from ``<role>_system.j2``."""
    return _RENDERER.render_template(f"{role}_system.j2")


__all__ = [
    "PROMPT_DIR",
    "render_archive_conflict",
    "render_continuation_prompt",
    "render_exhaustion_notice",
    "render_implementer_prompt",
    "render_judge_prompt",
    "render_pareto_frontier",
    "render_pareto_guard",
    "render_plan_prompt",
    "render_pre_round_prompt",
    "render_profiler_prompt",
    "render_regression_notice",
    "render_system_prompt",
    "render_turn_failed_feedback",
]
