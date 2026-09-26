"""Pydantic schemas shared by the agent-loop reply and search-plan layers.

This module used to hold every structured agent-reply schema; those moved to
``vibesys.roles.<family>`` (reply schemas next to their ``Role``) and
``vibesys.evaluators`` (measurement records: perf stats, validation recipes).
``OrchestratorPlan`` and ``HypothesisStrategyUpdate`` moved to
``vibesys.search.hypothesis.plan`` (the designer reply type IS the search plan
type). What remains here -- ``PerfTrend`` and ``SkillResourceSelection`` --
does not: ``PerfTrend`` is a runtime dependency of ``vibesys.evaluators``
(perf-eval reply schemas), and ``search`` already depends on
``vibesys.evaluators`` for ``MetricSpace``; moving ``PerfTrend`` into
``search.hypothesis`` would make that a dependency cycle, so it stays in this
dependency-free module instead (deviation from the design brief, which lists
``PerfTrend`` alongside ``OrchestratorPlan``). ``SkillResourceSelection``
stays here for the same reason ``OrchestratorPlan`` needed it here before its
move: it is shared by both ``search.hypothesis.plan`` and
``vibesys.roles.common``, and ``search`` must never depend on ``vibesys.roles``.

This module stays below roles, search, and loop-state records so shared values
can flow upward without creating a dependency cycle.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HypothesisOutcome(StrEnum):
    """Implementer-owned status for the active experimental hypothesis."""

    CONTINUE = "continue"
    SUPPORTED = "supported"
    NOMINATED = "nominated"
    DISPROVEN = "disproven"
    IMPLEMENTATION_FAILED = "implementation_failed"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"


class CandidateDisposition(StrEnum):
    """How a measured candidate should be retained independently of its hypothesis."""

    UNASSESSED = "unassessed"
    DISCARD = "discard"
    PREREQUISITE = "prerequisite"
    PARETO_FRONTIER = "pareto_frontier"


class PerfDeltaReason(StrEnum):
    """Why a headline measurement carries no causal delta."""

    NO_BASELINE_YET = "no_baseline_yet"
    BASELINE_UNRESOLVED = "baseline_unresolved"
    NOT_FRAMEWORK_MEASURED = "not_framework_measured"


class HypothesisResolution(StrEnum):
    """Resolution after all available hypothesis evidence is known."""

    PROVEN = "proven"
    DISPROVEN = "disproven"
    INCONCLUSIVE = "inconclusive"
    IMPLEMENTATION_FAILED = "implementation_failed"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    UNMEASURED = "unmeasured"


class PerfTrend(StrEnum):
    """Direction of observed performance change across rounds."""

    IMPROVED = "improved"
    REGRESSED = "regressed"
    MIXED = "mixed"


class SkillResourceSelection(BaseModel):
    """Advisory selection of resources from one installed agent skill.

    The outer loop can recommend these resources, while implementation and
    review agents remain free to select different installed skills.  Paths are
    relative to the named skill root and are resolved by the framework before
    they are shown to another agent.
    """

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1, description="Exact installed skill name.")
    resource_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Optional files relative to the skill root. An empty list selects "
            "the skill router without preselecting a resource."
        ),
    )
    purpose: str = Field(
        min_length=1,
        description="Short reason these resources may help with the current work.",
    )

    @field_validator("skill", "purpose")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            message = "must contain non-whitespace text"
            raise ValueError(message)
        return value


HYPOTHESIS_TITLE_MAX_LEN = 60


def truncate_hypothesis_title(text: str) -> str:
    """Truncate ``text`` to ``HYPOTHESIS_TITLE_MAX_LEN`` on a word boundary.

    Adds a trailing ellipsis when truncation occurs. ``text`` is assumed to
    already be stripped and whitespace-collapsed; this only shortens it.
    """
    if len(text) <= HYPOTHESIS_TITLE_MAX_LEN:
        return text
    truncated = text[: HYPOTHESIS_TITLE_MAX_LEN - 1]
    boundary = truncated.rfind(" ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return truncated.rstrip() + "…"


def normalize_hypothesis_title(title: str) -> str:
    """Strip, collapse internal whitespace, and truncate an orchestrator title.

    Returns ``""`` when ``title`` carries no text, so callers can tell an
    absent title apart from one that was merely whitespace.
    """
    collapsed = " ".join(title.split())
    return truncate_hypothesis_title(collapsed)


def derive_hypothesis_title(claim: str) -> str | None:
    """Derive a display title from a hypothesis claim.

    Takes the claim's first line, then its first sentence, strips a trailing
    period, and truncates to ``HYPOTHESIS_TITLE_MAX_LEN``. Returns ``None``
    when the claim carries no text, so callers can distinguish "no claim" from
    a claim that happened to produce an empty title.
    """
    stripped = claim.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    first_sentence = first_line.split(". ", 1)[0].rstrip(". ")
    collapsed = " ".join(first_sentence.split())
    if not collapsed:
        return None
    return truncate_hypothesis_title(collapsed)
