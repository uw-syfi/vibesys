"""Policy-owned values for the single-agent orchestration presets."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, FiniteFloat, model_validator

from vibesys.hypothesis import (
    CandidateDisposition,
    SkillResourceSelection,
)
from vibesys.inputs import ProfileGuidedInput
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.review import Verdict

_ProfileGuidedInput = ProfileGuidedInput


class SingleAgentRoundResponse(BaseModel):
    """One agent performs implementer + judge + profiler in a single shot.

    Used by the agent outer-loop's ``--inner-loop=single-agent`` ablation:
    instead of three specialist agents handing off through the framework,
    the same agent implements the round's task, runs the always-on
    correctness checks, and captures a profile, then returns the combined
    verdict.
    """

    summary: str = Field(description="What was implemented or changed this round.")
    expected_behavior: str = Field(
        description="What behavior the implementation should exhibit (server contract, etc.)."
    )
    self_review: str = Field(
        description="Self-review of correctness, accuracy, benchmark sanity, and reward-hack risk — same gates the judge would enforce."
    )
    feedback: str = Field(
        description="Concrete issues to fix on retry; empty when verdict is PASS."
    )
    verdict: Verdict = Field(
        description=(
            "PASS when the exact scoped outcome is supported and objective invariants, "
            "evidence identity, and resource lifecycle hold; this does not assert global "
            "completion. FAIL otherwise."
        )
    )
    bottlenecks: str = Field(description="Ranked profile bottlenecks with concrete numbers.")
    suggestions: str = Field(
        description="Actionable optimization suggestions for the next round, tied to bottlenecks."
    )
    profile_analysis: str = Field(description="Detailed interpretation of the captured profile.")
    perf_metric: FiniteFloat | None = Field(
        default=None,
        description="Headline perf metric from the benchmark (per OBJECTIVE.md). None if not measured.",
    )
    perf_unit: str | None = Field(
        default=None,
        description="Unit/field name for perf_metric (e.g. 'median_tok_per_sec'). None when perf_metric is None.",
    )
    candidate_disposition: CandidateDisposition = Field(
        default=CandidateDisposition.UNASSESSED,
        description="Independent provisional checkpoint-retention recommendation.",
    )
    candidate_metrics: dict[str, FiniteFloat] = Field(
        default_factory=dict,
        description="Objective values from one fresh directly comparable end-to-end row.",
    )
    candidate_evaluation_artifact: str | None = Field(
        default=None,
        description="Workspace-relative raw artifact supporting candidate_metrics.",
    )
    candidate_operating_point: str = Field(
        default="",
        description="Workload/load/configuration identity for candidate_metrics.",
    )
    candidate_retention_reason: str = Field(
        default="",
        description="Reason for retaining or discarding the candidate checkpoint.",
    )
    skill_context_updates: list[SkillResourceSelection] = Field(
        default_factory=list,
        description="New skill resources consulted or selected during this turn.",
    )


class SingleOptions(AgentOrchestrationOptions):
    """Strict production descriptor options for the profiling-off preset."""

    profile_guided: Literal[None] = None

    @model_validator(mode="after")
    def _registered_values(self) -> SingleOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported single-agent interface {self.interface!r}"
            raise ValueError(message)
        return self


class ProfileGuidedSingleOptions(AgentOrchestrationOptions):
    """Strict production descriptor options for the profiling-on preset."""

    profile_guided: _ProfileGuidedInput

    @model_validator(mode="after")
    def _registered_values(self) -> ProfileGuidedSingleOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported profile-guided single-agent interface {self.interface!r}"
            raise ValueError(message)
        return self


__all__ = ["ProfileGuidedSingleOptions", "SingleAgentRoundResponse", "SingleOptions"]
