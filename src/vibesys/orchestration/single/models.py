"""The reply of the single-agent implementer."""

from typing import Annotated

from pydantic import Field, FiniteFloat

from vibesys.hypothesis import (
    CandidateDisposition,
    DecidedFrom,
    SkillResourceSelection,
)
from vibesys.orchestration.review import Verdict
from vs_core.api.search import RevisionReply, markdown_prose


class SingleAgentRoundResponse(RevisionReply):
    """One agent performs implementer + judge + profiler in a single shot.

    Used by the agent outer-loop's ``--inner-loop=single-agent`` ablation:
    instead of three specialist agents handing off through the framework,
    the same agent implements the round's task, runs the always-on
    correctness checks, and captures a profile, then returns the combined
    verdict.
    """

    summary: str = Field(description=markdown_prose("What was implemented or changed this round."))
    expected_behavior: str = Field(
        description=markdown_prose(
            "What behavior the implementation should exhibit (server contract, etc.)."
        )
    )
    self_review: str = Field(
        description=markdown_prose(
            "Self-review of correctness, accuracy, benchmark sanity, and reward-hack risk — "
            "same gates the judge would enforce."
        )
    )
    feedback: str = Field(
        description=markdown_prose("Concrete issues to fix on retry; empty when verdict is PASS.")
    )
    verdict: Annotated[Verdict, DecidedFrom("pass_criteria", "invariants")] = Field(
        description=(
            "PASS when the exact scoped outcome is supported and objective invariants, "
            "evidence identity, and resource lifecycle hold; this does not assert global "
            "completion. FAIL otherwise."
        )
    )
    bottlenecks: str = Field(
        description=markdown_prose("Ranked profile bottlenecks with concrete numbers.")
    )
    suggestions: str = Field(
        description=markdown_prose(
            "Actionable optimization suggestions for the next round, tied to bottlenecks."
        )
    )
    profile_analysis: str = Field(
        description=markdown_prose("Detailed interpretation of the captured profile.")
    )
    perf_metric: FiniteFloat | None = Field(
        default=None,
        description="Headline perf metric from the benchmark (per OBJECTIVE.md). None if not measured.",
    )
    perf_unit: str | None = Field(
        default=None,
        description="Unit/field name for perf_metric (e.g. 'median_tok_per_sec'). None when perf_metric is None.",
    )
    candidate_disposition: Annotated[
        CandidateDisposition, DecidedFrom("minimum_ratio", "dominated_by_rounds")
    ] = Field(
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
        description=markdown_prose("Reason for retaining or discarding the candidate checkpoint."),
    )
    skill_context_updates: list[SkillResourceSelection] = Field(
        default_factory=list,
        description="New skill resources consulted or selected during this turn.",
    )


__all__ = ["SingleAgentRoundResponse"]
