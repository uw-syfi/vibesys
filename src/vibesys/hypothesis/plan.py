"""The designer's round plan: the input to the hypothesis search.

``OrchestratorPlan`` is both the designer's structured reply schema and the
search plan type: the framework applies it in this order: the base the
implementer starts from (``base_round``, else the current best), then
implementer with ``task``, then judge with ``pass_criteria``. The skill recommendations and title normalization live
here because they are parts of this plan's agent-facing contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vs_core.api import markdown_prose, plain_line
from vs_issue_tracker.api import RoadmapItem
from vs_runtime.api import AgentId

HypothesisStrategyDisposition = Literal["parked", "abandoned"]

HYPOTHESIS_TITLE_MAX_LEN = 60


class InvalidPlanError(ValueError):
    """The designer returned an invalid hypothesis state transition."""

    def __init__(self, detail: str) -> None:
        """Name the violated plan invariant."""
        super().__init__(detail)

    @classmethod
    def duplicate_updates(cls) -> InvalidPlanError:
        """Report repeated prior hypothesis IDs."""
        return cls("hypothesis_updates names one hypothesis more than once")

    @classmethod
    def self_reference(cls) -> InvalidPlanError:
        """Report a plan that updates its own new hypothesis."""
        return cls("hypothesis_updates includes the new hypothesis")

    @classmethod
    def reused_id(cls, hypothesis_id: str) -> InvalidPlanError:
        """Report a new hypothesis ID already used in this run."""
        return cls(f"hypothesis ID {hypothesis_id!r} was already used")

    @classmethod
    def unknown_base(cls, base_round: int, passed: tuple[int, ...]) -> InvalidPlanError:
        """Report a base that names no idea this run has played."""
        return cls(f"base_round {base_round} names no idea of this run; {_choices(passed)}")

    @classmethod
    def failed_base(cls, base_round: int, passed: tuple[int, ...]) -> InvalidPlanError:
        """Report a base that names an idea that did not pass."""
        return cls(f"base_round {base_round} names an idea that did not pass; {_choices(passed)}")


def _choices(passed: tuple[int, ...]) -> str:
    """The bases a plan may name, for the message that refuses another."""
    named = ", ".join(str(number) for number in passed) or "none yet"
    return f"base_round may be 0 (the input) or a passed idea ({named}), or omitted for the best"


class SkillResourceSelection(BaseModel):
    """Advisory selection of resources from one installed agent skill.

    The planner can recommend these resources while implementation and review
    agents remain free to select different installed skills. Paths are relative
    to the named skill root and are resolved before another agent sees them.
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


def truncate_hypothesis_title(text: str) -> str:
    """Truncate a normalized title on a word boundary, adding an ellipsis."""
    if len(text) <= HYPOTHESIS_TITLE_MAX_LEN:
        return text
    truncated = text[: HYPOTHESIS_TITLE_MAX_LEN - 1]
    boundary = truncated.rfind(" ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return truncated.rstrip() + "…"


def normalize_hypothesis_title(title: str) -> str:
    """Strip, collapse internal whitespace, and truncate a plan title.

    Return an empty string when the input carries no text.
    """
    return truncate_hypothesis_title(" ".join(title.split()))


def derive_hypothesis_title(claim: str) -> str | None:
    """Derive a display title from a hypothesis claim's first sentence.

    Return ``None`` when the claim carries no text.
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


class HypothesisStrategyUpdate(BaseModel):
    """One structured update to a previously completed hypothesis."""

    model_config = ConfigDict(extra="forbid")

    # Exact: it is matched against recorded hypothesis IDs, never normalized.
    hypothesis_id: AgentId
    disposition: HypothesisStrategyDisposition
    reason: str = Field(
        min_length=1,
        description=markdown_prose("Why this hypothesis is parked or abandoned."),
    )

    @field_validator("reason")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        if not (stripped := value.strip()):
            message = "must not be blank"
            raise ValueError(message)
        return stripped


class OrchestratorPlan(BaseModel):
    """The per-round plan produced by the orchestrator.

    The framework applies the plan in this order: the base the implementer
    starts from (``base_round``, else the current best), then implementer with
    ``task``, then judge with ``pass_criteria``. The loop always runs the full
    ``max_rounds`` budget; there is no early-stop signal from the
    orchestrator.
    """

    model_config = ConfigDict(extra="forbid")

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
            "These are persisted by the framework; roadmap items do not replace them."
        ),
    )
    roadmap: list[RoadmapItem] = Field(
        default_factory=list,
        description=(
            "Roadmap items to create or replace, each by its id; list only new or changed "
            "items, each in full. Items are never deleted: mark an ended direction done, "
            "parked or abandoned."
        ),
    )
    hypothesis: str = Field(
        default="",
        description=markdown_prose(
            "Causal, falsifiable claim explaining why the proposed change should help."
        ),
    )
    title: str = Field(
        default="",
        description=plain_line(
            f"Short plain-language title (<={HYPOTHESIS_TITLE_MAX_LEN} chars, no "
            f"trailing period or 'Hypothesis:' prefix)."
        ),
    )
    activation_evidence: str = Field(
        default="",
        description=markdown_prose(
            "Concise observable evidence that the intended code path or mechanism "
            "ran; include only hypothesis-specific signals."
        ),
    )
    falsification_criteria: str = Field(
        default="",
        description=markdown_prose(
            "Evidence that would disprove the causal hypothesis for this workload."
        ),
    )
    expected_effect: str = Field(
        default="",
        description=markdown_prose(
            "Analytical forecast range used to prioritize and later calibrate the "
            "hypothesis; this is not an acceptance threshold."
        ),
    )
    minimum_acceptance_criteria: str = Field(
        default="",
        description=markdown_prose(
            "Minimum observed end-to-end benefit and allowed tradeoffs that justify "
            "retaining the change, derived separately from forecast uncertainty, "
            "benchmark noise, and implementation cost."
        ),
    )
    invariants: str = Field(
        default="",
        description=markdown_prose(
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
        description=markdown_prose(
            "Causally complete component/interface and staged-evaluation delta handed "
            "to the implementer, without restating stable authoritative contracts."
        )
    )
    pass_criteria: str = Field(
        description=markdown_prose(
            "Hypothesis-specific activation, correctness, cleanup, and evidence gates. "
            "Reference stable trusted gates by path; they run separately when configured."
        )
    )
    base_round: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional base: the number of a passed idea whose code this idea starts from; "
            "0 is the input. Omit it to start from the current best. Experiment memory is "
            "preserved either way."
        ),
    )
    reasoning: str = Field(
        description=markdown_prose(
            "Brief decisive comparison supporting this round's parent and mechanism."
        )
    )
