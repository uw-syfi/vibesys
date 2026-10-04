"""Prompt rendering for the dynamic orchestration.

Every text the dynamic plugin sends to an agent, including corrections and the
feedback a retry receives, is rendered from a template in this directory.
Callers pass data; the templates own the wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vs_prompts.api import RenderedPrompt, TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.orchestration.dynamic.lifecycle import TimedOut
    from vibesys.orchestration.dynamic.models import SteerNote
    from vs_evaluator_protocol.api import ProfileField

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


@dataclass(frozen=True, slots=True)
class RepeatedFailureLine:
    """Projection of the established evaluator guidance into template data."""

    kind: Literal["traceback", "measurement"]
    stage: Literal["accuracy", "benchmark", "profile"] | None
    signature: str
    count: int
    instruction: str


@dataclass(frozen=True, slots=True)
class EvaluationResumeLine:
    """Trusted observation and immutable measurement references for one handle."""

    handle_id: str
    status: str
    candidate_revision: str
    evaluator_revision: str
    evidence_ids: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    detail: str
    diagnostics: tuple[str, ...] = ()
    repeated_failure: RepeatedFailureLine | None = None


def render_evaluation_resume(
    *,
    role: Literal["implementer", "judge"],
    retained_revision: str,
    results: Sequence[EvaluationResumeLine],
    notes: Sequence[SteerNote] = (),
    timed_out: TimedOut | None = None,
) -> RenderedPrompt:
    """Resume the original role with trusted observations and reserved steers."""
    return _RENDERER.render_template(
        "resume.j2",
        role=role,
        retained_revision=retained_revision,
        results=results,
        notes=notes,
        interrupted_revision=None,
        timed_out=timed_out,
    )


def render_evaluation_wait_error(*, error: str) -> RenderedPrompt:
    """Return a typed wait authorization error to the completed conversation."""
    return _RENDERER.render_template("wait_error.j2", error=error)


def render_evaluation_resume_bound(repeated: RepeatedFailureLine) -> RenderedPrompt:
    """Explain the typed failure that ended a charged attempt across its continuations."""
    return _RENDERER.render_template("feedback_resume_bound.j2", repeated=repeated)


def render_evaluation_no_progress() -> RenderedPrompt:
    """Explain why re-yielding only known handles cannot authorize another turn."""
    return _RENDERER.render_template("feedback_no_progress.j2")


def render_evaluation_history_unavailable() -> RenderedPrompt:
    """Explain explicit migration for an old attempt lacking a durable cursor."""
    return _RENDERER.render_template("feedback_history_unavailable.j2")


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


def render_implementation(
    *, notes: Sequence[SteerNote], interrupted_revision: str | None, **context: object
) -> RenderedPrompt:
    """Render one isolated hypothesis implementation request.

    ``notes`` are the orchestrator's steers delivered to this turn.
    ``interrupted_revision`` is the work-in-progress revision kept when the
    previous turn was ended early to deliver them, or ``None``.
    """
    return _RENDERER.render_template(
        "implement.j2", notes=notes, interrupted_revision=interrupted_revision, **context
    )


def render_profile_request(**context: object) -> RenderedPrompt:
    """Render the request a profile workstream sends to the run's profiler agent."""
    return _RENDERER.render_template("profile_request.j2", **context)


def render_profile_fields_unavailable(
    *, position: int, required_fields: Sequence[ProfileField]
) -> RenderedPrompt:
    """Name measurement requirements already unavailable from the configured capture."""
    return _RENDERER.render_template(
        "profile_fields_unavailable.j2", position=position, required_fields=required_fields
    )


def render_review(
    *, evaluations: Sequence[EvaluationLine], notes: Sequence[SteerNote], **context: object
) -> RenderedPrompt:
    """Render one independent candidate review request with the steers delivered to it."""
    return _RENDERER.render_template("review.j2", evaluations=evaluations, notes=notes, **context)


def render_steer_dropped(*, note_sha256: str, sent_at_s: float) -> RenderedPrompt:
    """Render the journal text recording a steer dropped because its workstream settled."""
    return _RENDERER.render_template(
        "steer_dropped.j2", note_sha256=note_sha256, sent_at_s=sent_at_s
    )


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
    "EvaluationResumeLine",
    "FailureTail",
    "RepeatedFailureLine",
    "render_agent_failures_feedback",
    "render_evaluation_history_unavailable",
    "render_evaluation_no_progress",
    "render_evaluation_resume",
    "render_evaluation_resume_bound",
    "render_evaluation_wait_error",
    "render_implementation",
    "render_portfolio",
    "render_portfolio_correction",
    "render_profile_fields_unavailable",
    "render_profile_request",
    "render_repeated_failure_feedback",
    "render_review",
    "render_steer_dropped",
    "render_system_prompt",
    "render_trusted_evaluation_feedback",
]
