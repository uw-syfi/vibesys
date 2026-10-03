"""Policy-owned values for the single-agent orchestration presets."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from vibesys.inputs import ProfileGuidedInput
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.hypothesis import (
    ExhaustionNotice,
    RegressionNotice,
    SkillResourceSelection,
)
from vibesys.orchestration.hypothesis.state import HypothesisState
from vibesys.orchestration.profile_focus import FocusLedger
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vs_loop_state.api import CandidateDisposition
from vs_runtime.api import AccuracyReceipt

_ProfileGuidedInput = ProfileGuidedInput


class PlanContext(BaseModel):
    """Changing evidence rendered for one single-policy planning turn."""

    model_config = ConfigDict(frozen=True)

    objective_location: str
    profiler_summary: ProfilerSummary | None
    regression_info: RegressionNotice | None
    exhaustion_info: ExhaustionNotice | None
    progress_location: str
    roadmap_location: str
    pareto_archive_location: str
    plateau_warning: str | None
    domain_orchestrator: str
    runtime_notes: str
    framework_benchmark_enabled: bool
    official_eval_every: int
    provisional_candidates: int
    official_eval_cadence_due: bool
    active_component: str | None = None
    ledger: FocusLedger | None = None
    ranked_bottlenecks: list[dict[str, object]] = Field(default_factory=list)


class SingleAgentRoundContext(BaseModel):
    """Changing evidence for one combined implementation and review turn."""

    model_config = ConfigDict(frozen=True)

    accuracy_command: str | None
    benchmark_command: str | None
    domain_profiler: str
    domain_single_agent: str
    feedback: str | None
    interface: str
    objective_location: str
    official_evaluation_due: bool
    official_evaluation_reason: str | None
    pareto_archive_location: str
    plan_artifact_location: str
    profiler_kind: str
    profiler_support_name: str | None
    progress_location: str
    runtime_notes: str
    validation_location: str


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
        description="PASS if all gates (orchestrator pass criteria + always-on checks) hold; FAIL otherwise."
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


class PaidAttempt(BaseModel):
    """Durable proof that one implementer attempt was started."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round_number: Annotated[int, Field(gt=0)]
    role_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)
    turn_number: Annotated[int, Field(gt=0)]


class SingleState(BaseModel):
    """The single plugin's complete opaque durability aggregate."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    search: HypothesisState = Field(default_factory=HypothesisState)
    last_paid_attempt: PaidAttempt | None = None
    accuracy_receipt: AccuracyReceipt | None = None
    last_response: SingleAgentRoundResponse | None = None


__all__ = [
    "PaidAttempt",
    "PlanContext",
    "ProfileGuidedSingleOptions",
    "SingleAgentRoundContext",
    "SingleAgentRoundResponse",
    "SingleOptions",
    "SingleState",
]
