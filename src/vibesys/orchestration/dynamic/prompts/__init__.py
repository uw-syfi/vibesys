"""Prompt rendering for the dynamic orchestration.

Every text the dynamic plugin sends to an agent, including corrections and the
feedback a retry receives, is rendered from a template in this directory.
Callers pass data; the templates own the wording.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vs_prompts.api import RenderedPrompt, TemplateRenderer

_RENDERER = TemplateRenderer(Path(__file__).parent)


@dataclass(frozen=True, slots=True)
class FailureTail:
    """The end of a failure text, and whether its start was cut."""

    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class EvaluationLine:
    """One agent-submitted evaluation as a prompt lists it.

    ``failure`` is ``None`` when the evaluation recorded no failure text.
    """

    revision: str
    kinds: tuple[str, ...]
    status: str
    failure: FailureTail | None


def render_system_prompt(role: str) -> RenderedPrompt:
    """Render the fixed system prompt of one agent role from ``<role>_system.j2``."""
    return _RENDERER.render_template(f"{role}_system.j2")


def render_portfolio(**context: object) -> RenderedPrompt:
    """Render one compact portfolio planning request."""
    return _RENDERER.render_template("portfolio.j2", **context)


def render_portfolio_correction(
    *, error: str | None, scheduled: int, **context: object
) -> RenderedPrompt:
    """Render the planning request again with why the last plan was not accepted.

    ``error`` names the plan's validation error. ``None`` means the plan was
    valid but scheduled only ``scheduled`` of the free slots, and asks once to
    fill them.
    """
    return _RENDERER.render_template(
        "portfolio_correction.j2", error=error, scheduled=scheduled, **context
    )


def render_implementation(**context: object) -> RenderedPrompt:
    """Render one isolated hypothesis implementation request."""
    return _RENDERER.render_template("implement.j2", **context)


def render_review(*, evaluations: Sequence[EvaluationLine], **context: object) -> RenderedPrompt:
    """Render one independent candidate review request."""
    return _RENDERER.render_template("review.j2", evaluations=evaluations, **context)


def render_agent_failures_feedback(
    *, feedback: str | None, evaluations: Sequence[EvaluationLine]
) -> RenderedPrompt:
    """Append an attempt's failed evaluations to its correction guidance."""
    return _RENDERER.render_template(
        "feedback_agent_failures.j2", feedback=feedback, evaluations=evaluations
    )


def render_repeated_failure_feedback(
    *, limit: int, signature: str, failure: FailureTail
) -> RenderedPrompt:
    """Explain that ``limit`` identical failures in a row ended the attempt."""
    return _RENDERER.render_template(
        "feedback_repeated_failure.j2", limit=limit, signature=signature, failure=failure
    )


def render_trusted_evaluation_feedback(messages: Sequence[str]) -> RenderedPrompt:
    """List the messages of the trusted gates that failed."""
    return _RENDERER.render_template("feedback_trusted_evaluation.j2", messages=list(messages))


__all__ = [
    "EvaluationLine",
    "FailureTail",
    "render_agent_failures_feedback",
    "render_implementation",
    "render_portfolio",
    "render_portfolio_correction",
    "render_repeated_failure_feedback",
    "render_review",
    "render_system_prompt",
    "render_trusted_evaluation_feedback",
]
