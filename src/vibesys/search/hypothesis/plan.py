"""The designer's round plan: the input to the hypothesis search.

``OrchestratorPlan`` is both the designer's structured reply schema and the
search plan type: the framework applies it in this order: optional
``revert_to_round`` git checkout, then implementer with ``task``, then judge
with ``pass_criteria``. Moved from ``vibesys.schemas`` (see that module's
docstring for what stays there and why).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vibesys.schemas import (
    HYPOTHESIS_TITLE_MAX_LEN,
    SkillResourceSelection,
)

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
            message = "must not be blank"
            raise ValueError(message)
        return stripped


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
