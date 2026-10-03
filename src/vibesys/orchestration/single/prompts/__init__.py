"""Prompt rendering owned by the single-agent orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.orchestration.prompts import PROMPTS_DIR as SHARED_PROMPTS_DIR
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from vibesys.orchestration.hypothesis import (
        ArchiveConflict,
        ExhaustionNotice,
        ParetoArchiveView,
        RegressionNotice,
    )
    from vibesys.orchestration.single.models import PlanContext, SingleAgentRoundContext

PROMPT_DIR = Path(__file__).resolve().parent
# The execution boundary is shared by the legacy policies until they migrate.
_RENDERER = TemplateRenderer(PROMPT_DIR, fallback_roots=(SHARED_PROMPTS_DIR / "shared",))


def render_plan_prompt(context: PlanContext) -> str:
    """Render the complete legacy designer brief as changing turn content."""
    return _RENDERER.render_template("orchestrator_plan_prompt.j2", **context.model_dump())


def render_single_agent_prompt(context: SingleAgentRoundContext) -> str:
    """Render the complete legacy implementer brief, including shared execution rules."""
    return _RENDERER.render_template("single_agent_round_prompt.j2", **context.model_dump())


def render_archive_conflict(conflict: ArchiveConflict) -> str:
    """Render the live-archive conflict notice a guarded verdict carries as feedback."""
    return _RENDERER.render_template(
        "_notices/archive_conflict.j2", pareto_archive_conflict=conflict
    )


def render_pareto_guard(review: str, conflict: ArchiveConflict) -> str:
    """Render the self-review after the framework's Pareto guard overrode a pass."""
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


def render_system_prompt(role: str) -> str:
    """Render the fixed system prompt of one agent role from ``<role>_system.j2``."""
    return _RENDERER.render_template(f"{role}_system.j2")


__all__ = [
    "PROMPT_DIR",
    "render_archive_conflict",
    "render_exhaustion_notice",
    "render_pareto_frontier",
    "render_pareto_guard",
    "render_plan_prompt",
    "render_regression_notice",
    "render_single_agent_prompt",
    "render_system_prompt",
]
