"""Single-agent role family: one agent does implement + judge + profile.

Used by the ``single`` strategy's inner-loop ablation, in both its plain
and profile-guided (``profile-guided-single-agent``) presets. Both presets
render the same template and reply shape; profiling is a context value
(``ctx.options.profile_guided``), not a different role.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from framework.api import SessionScope
from vibesys.loops.state.api import CandidateDisposition
from vibesys.roles.common import SkillResourceSelection, Verdict
from vibesys.runtime import Keyed, Role, Writes


class SingleAgentRoundContext(BaseModel):
    """Shared context for the single-agent combined role.

    ``single`` serves both its plain and profile-guided presets from this
    one context model and template.
    """

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


def _fallback_combined() -> SingleAgentRoundResponse:
    """Used when the structured reply could not be parsed."""
    return SingleAgentRoundResponse(
        summary="Single-agent produced no structured response.",
        expected_behavior="unknown",
        self_review="No structured response received.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
        candidate_disposition=CandidateDisposition.UNASSESSED,
    )


def _timeout_fallback_combined(timeout: float) -> SingleAgentRoundResponse:
    """Used when the turn hit ``subprocess.TimeoutExpired`` (a6e361c1 text)."""
    return SingleAgentRoundResponse(
        summary="Single-agent invocation timed out.",
        expected_behavior="unknown",
        self_review=(
            f"The framework stopped the agent after {timeout:g} seconds "
            "without a structured response."
        ),
        feedback="Inspect retained evidence and return a schema-valid response on retry.",
        verdict=Verdict.FAIL,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
        candidate_disposition=CandidateDisposition.UNASSESSED,
    )


SINGLE_COMBINED = Role(
    id="implementer",
    template="loops/single/single_agent_round_prompt.j2",
    reply=SingleAgentRoundResponse,
    fallback=_fallback_combined,
    context=SingleAgentRoundContext,
    timeout_fallback=_timeout_fallback_combined,
    access=Writes(),
    session=Keyed(scope=SessionScope.HYPOTHESIS),
    paid=True,
    filter_skills=True,
    message=(
        "Carry out the orchestrator's task above end-to-end "
        "(implement, self-judge, profile) and return only the JSON object."
    ),
)

ALL_ROLES = (SINGLE_COMBINED,)
