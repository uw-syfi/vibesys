"""Pydantic schemas for the round-planning ("designer") reply.

This module used to hold every structured agent-reply schema; those moved to
``vibesys.roles.<family>`` (reply schemas next to their ``Role``) and
``vibesys.evaluators`` (measurement records: perf stats, validation recipes).
What remains -- ``OrchestratorPlan``, ``HypothesisStrategyUpdate``,
``PerfTrend``, and ``SkillResourceSelection`` -- is search-plan data destined
for ``search/hypothesis`` (the designer reply type IS the search plan type);
it stays here until that move lands.

Deviation from the design brief: ``SkillResourceSelection`` is listed there as
moving to ``vibesys.roles.common``, but ``OrchestratorPlan.recommended_skills``
uses it and ``OrchestratorPlan`` stays here (lane A's territory) pending its
own move to the layering-pure ``search/hypothesis``, which must not depend on
``vibesys.roles`` (roles depends on search, never the reverse). Its canonical
definition therefore stays here; ``vibesys.roles.common`` imports and
re-exports it for role reply schemas that also need it.

This module has no local imports besides the dependency-free ``vs_loop_state``
leaf lib, so templates and tests can pull schemas in without dragging in the
rest of the agent runtime.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vs_loop_state.api import (
    CandidateDisposition,  # noqa: F401
    HypothesisOutcome,  # noqa: F401
    PerfDeltaReason,  # noqa: F401
)

# HypothesisOutcome, CandidateDisposition, and PerfDeltaReason live in
# vs_loop_state so that server code can import them without deep-importing
# vibesys internals. Re-exported here so existing call sites outside this
# refactor's scope keep working unchanged.

HypothesisStrategyDisposition = Literal["parked", "abandoned"]


class HypothesisStrategyUpdate(BaseModel):
    """One structured update to a previously completed hypothesis."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1)
    disposition: HypothesisStrategyDisposition
    reason: str = Field(min_length=1)

    @field_validator("hypothesis_id", "reason")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        if not (stripped := value.strip()):
            raise ValueError("must not be blank")  # noqa: TRY003  # tracked: #288
        return stripped


class PerfTrend(StrEnum):  # noqa: D101  # tracked: #288
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
            raise ValueError("must contain non-whitespace text")  # noqa: TRY003  # tracked: #288
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


class OrchestratorPlan(BaseModel):
    """The per-round plan produced by the orchestrator.

    The framework applies the plan in this order: optional
    ``revert_to_round`` git checkout, then implementer with ``task``,
    then judge with ``pass_criteria``. The loop always runs the full
    ``max_rounds`` budget; there is no early-stop signal from the
    orchestrator.
    """

    hypothesis_id: str = Field(
        default="",
        description=(
            "Stable short identifier for this round's new hypothesis. Never reuse "
            "an identifier used earlier in this run."
        ),
    )
    hypothesis_updates: list[HypothesisStrategyUpdate] = Field(
        default_factory=list,
        description=(
            "Strategic parked/abandoned updates for previously completed hypotheses. "
            "These are persisted by the framework; roadmap prose is not authoritative."
        ),
    )
    hypothesis: str = Field(
        default="",
        description="Causal, falsifiable claim explaining why the proposed change should help.",
    )
    title: str = Field(
        default="",
        description=(
            f"Short plain-language title (<={HYPOTHESIS_TITLE_MAX_LEN} chars, no "
            f"trailing period or 'Hypothesis:' prefix)."
        ),
    )
    activation_evidence: str = Field(
        default="",
        description=(
            "Concise observable evidence that the intended code path or mechanism "
            "ran; include only hypothesis-specific signals."
        ),
    )
    falsification_criteria: str = Field(
        default="",
        description="Evidence that would disprove the causal hypothesis for this workload.",
    )
    expected_effect: str = Field(
        default="",
        description=(
            "Analytical forecast range used to prioritize and later calibrate the "
            "hypothesis; this is not an acceptance threshold."
        ),
    )
    minimum_acceptance_criteria: str = Field(
        default="",
        description=(
            "Minimum observed end-to-end benefit and allowed tradeoffs that justify "
            "retaining the change, derived separately from forecast uncertainty, "
            "benchmark noise, and implementation cost."
        ),
    )
    invariants: str = Field(
        default="",
        description=(
            "Hypothesis-specific invariants and diagnostics. Cite authoritative "
            "objective/runtime paths for stable constraints instead of restating them."
        ),
    )
    recommended_skills: list[SkillResourceSelection] = Field(
        default_factory=list,
        description=(
            "Zero or more installed skill resources that may help implement this "
            "hypothesis. Recommendations are advisory, not an allowlist or gate."
        ),
    )
    task: str = Field(
        description=(
            "Causally complete component/interface and staged-evaluation delta handed "
            "to the implementer, without restating stable authoritative contracts."
        )
    )
    pass_criteria: str = Field(
        description=(
            "Hypothesis-specific activation, correctness, cleanup, and evidence gates. "
            "Reference stable trusted gates by path; they run separately when configured."
        )
    )
    request_official_evaluation: bool = Field(
        default=False,
        description=(
            "Run the framework-owned canonical evaluation after this hypothesis "
            "passes independent review, even when the normal sparse-evaluation "
            "cadence is not yet due."
        ),
    )
    revert_to_round: int | None = Field(
        default=None,
        description=(
            "Optional round number whose candidate tree the framework should "
            "materialize before the implementer runs. The framework preserves "
            "durable experiment memory and does not move Git HEAD."
        ),
    )
    reasoning: str = Field(
        description="Brief decisive comparison supporting this round's parent and mechanism."
    )
