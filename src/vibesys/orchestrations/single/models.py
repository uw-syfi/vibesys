"""Policy-owned values for the single-agent orchestration slice."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SingleOptions(BaseModel):
    """Strict options understood by the first single-agent policy slice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1)
    max_retries_per_round: int = Field(default=2, ge=0)


class HypothesisUpdate(BaseModel):
    """One lifecycle update proposed for an earlier hypothesis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SinglePlan(BaseModel):
    """A designer-selected hypothesis and bounded implementation task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    task: str = Field(min_length=1)
    pass_criteria: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    hypothesis_updates: tuple[HypothesisUpdate, ...] = ()


class Verdict(StrEnum):
    """The combined implementer and self-review decision."""

    APPROVE = "approve"
    REVISE = "revise"


class SingleAgentResult(BaseModel):
    """Combined implementation and self-review response for one attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    self_review: str = Field(min_length=1)
    feedback: str = ""
    verdict: Verdict


class InvalidSinglePlanError(ValueError):
    """A parsed designer plan violates hypothesis lifecycle policy."""

    @classmethod
    def duplicate_updates(cls) -> InvalidSinglePlanError:
        """Describe repeated lifecycle edits for one prior hypothesis."""
        return cls("hypothesis_updates contains duplicate identifiers")

    @classmethod
    def self_update(cls) -> InvalidSinglePlanError:
        """Describe a new hypothesis attempting to update itself."""
        return cls("a new hypothesis cannot update itself")


def fallback_plan() -> SinglePlan:
    """Choose the conservative plan used after an unparseable designer reply."""
    return SinglePlan.model_validate(
        {
            "hypothesis_id": "fallback-health-check",
            "hypothesis": "the minimal service path may not satisfy its health contract",
            "task": "Re-check that the minimal server boots and its health endpoint responds.",
            "pass_criteria": "The health endpoint returns a successful response.",
            "reasoning": "fallback: the designer produced no structured response",
        }
    )


def fallback_result() -> SingleAgentResult:
    """Treat an unparseable implementation reply as a failed self-review."""
    return SingleAgentResult(
        summary="The agent produced no structured implementation response.",
        expected_behavior="unknown",
        self_review="No structured response was available to review.",
        feedback="Return a complete schema-valid response on retry.",
        verdict=Verdict.REVISE,
    )


__all__ = [
    "HypothesisUpdate",
    "InvalidSinglePlanError",
    "SingleAgentResult",
    "SingleOptions",
    "SinglePlan",
    "Verdict",
    "fallback_plan",
    "fallback_result",
]
