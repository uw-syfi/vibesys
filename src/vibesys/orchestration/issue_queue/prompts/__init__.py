"""Issue-queue agent prompts, rendered from the templates beside this module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from vs_prompts.api import RenderedPrompt, TemplateRenderer

if TYPE_CHECKING:
    from vibesys.orchestration.issue_queue.models import IssueQueueOptions, IssueQueueState
    from vs_issue_tracker.api import Issue
    from vs_runtime.api import RunFacts

_RENDERER = TemplateRenderer(Path(__file__).parent)


def bootstrap_description(facts: RunFacts) -> RenderedPrompt:
    """Describe the first candidate-facing implementation issue."""
    return _RENDERER.render_template("bootstrap_description.j2", facts=facts)


def implementer_message(
    issue: Issue, facts: RunFacts, prior_review: dict[str, object] | None
) -> RenderedPrompt:
    """Request one bounded implementation attempt."""
    return _RENDERER.render_template(
        "implementer_message.j2", issue=issue, facts=facts, prior_review=prior_review
    )


def judge_message(issue: Issue, facts: RunFacts) -> RenderedPrompt:
    """Request an independent functional review."""
    return _RENDERER.render_template("judge_message.j2", issue=issue, facts=facts)


def performance_message(
    *,
    iteration: int,
    facts: RunFacts,
    options: IssueQueueOptions,
    state: IssueQueueState,
) -> RenderedPrompt:
    """Request one measured performance assessment and bounded queue update."""
    load_levels = (
        [level.model_dump(mode="json") for level in options.load_levels]
        if options.load_levels is not None
        else None
    )
    prior = [record.model_dump(mode="json") for record in state.performance]
    return _RENDERER.render_template(
        "performance_message.j2",
        iteration=iteration,
        facts=facts,
        max_issues_per_perf_eval=options.max_issues_per_perf_eval,
        load_levels_json=None if load_levels is None else json.dumps(load_levels, sort_keys=True),
        prior_records_json=json.dumps(prior, sort_keys=True),
    )


def implementer_system_prompt() -> RenderedPrompt:
    """Return the implementer role's standing instructions."""
    return _RENDERER.render_template("implementer_system_prompt.j2")


def judge_system_prompt() -> RenderedPrompt:
    """Return the judge role's standing instructions."""
    return _RENDERER.render_template("judge_system_prompt.j2")


def performance_system_prompt() -> RenderedPrompt:
    """Return the performance evaluator role's standing instructions."""
    return _RENDERER.render_template("performance_system_prompt.j2")


__all__ = [
    "bootstrap_description",
    "implementer_message",
    "implementer_system_prompt",
    "judge_message",
    "judge_system_prompt",
    "performance_message",
    "performance_system_prompt",
]
