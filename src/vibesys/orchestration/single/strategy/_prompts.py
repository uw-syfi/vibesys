"""Typed data the strategy hands to the declared prompt-rendering operation.

The strategy never builds prompt text. It sends a `PromptContext`; the operation's executor
renders the named template with `vs_prompts` and stores the artifacts. The evidence the legacy
loop kept in progress files (round history, the Pareto frontier, carried notices, the last
profile) is rendered from the typed rows below, because a round's workspace is an isolated child
of a revision and holds no progress files.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from vibesys.prompts import ReplyCorrectionPrompt, ResumePrompt, WaitUnrecordedPrompt
from vs_core.api import RevisionRef, Value


class PromptTemplate(StrEnum):
    """Closed set of single's own templates (the shared protocol ones are `CoreProtocolTemplate`)."""

    PLAN = "plan"
    ROUND = "round"


class RoundRow(Value):
    """One finished round, as the designer's history shows it."""

    round_number: int = Field(gt=0)
    hypothesis_id: str
    claim: str = ""
    passed: bool
    official: bool
    judge_verdict: str
    outcome: str | None = None
    metric: float | None = None
    unit: str | None = None
    delta_pct: float | None = None
    retained: bool | None = None
    commit: str | None = None


class FrontierRow(Value):
    """A trusted point on the noise-aware Pareto frontier."""

    round_number: int = Field(gt=0)
    commit: str
    official: bool
    metrics: tuple[tuple[str, float], ...] = ()
    operating_point: str = ""


class PendingClaim(Value):
    """A retained frontier claim that is not yet a trusted parent."""

    round_number: int = Field(gt=0)
    commit: str
    metrics: tuple[tuple[str, float], ...] = ()
    reason: str = ""


class Bottleneck(Value):
    """One ranked component of the measured attribution."""

    component: str
    cost_share_pct: float
    evidence: tuple[str, ...] = ()


class LastProfile(Value):
    """The profile the previous round's implementer reported."""

    analysis: str = ""
    bottlenecks: str = ""
    suggestions: str = ""
    perf_metric: float | None = None
    perf_unit: str | None = None


class PlanPrompt(Value):
    """One new-hypothesis planning request."""

    template: Literal[PromptTemplate.PLAN] = PromptTemplate.PLAN
    round_number: int = Field(gt=0)
    max_rounds: int = Field(gt=0)
    plateau_warning: str | None = None
    provisional_candidates: int = Field(default=0, ge=0)
    official_eval_every: int = Field(default=1, gt=0)
    official_cadence_due: bool = False
    benchmark_enabled: bool = True
    history: tuple[RoundRow, ...] = ()
    older_rounds: int = Field(default=0, ge=0)
    frontier: tuple[FrontierRow, ...] = ()
    pending_claims: tuple[PendingClaim, ...] = ()
    last_profile: LastProfile | None = None
    regression_round: int | None = None
    regression_kind: str | None = None
    exhaustion_feedback: str | None = None
    exhaustion_round: int | None = None
    active_component: str | None = None
    ranked_bottlenecks: tuple[Bottleneck, ...] = ()
    base_revision: RevisionRef


class RoundPrompt(Value):
    """One implementation, self-review and profile request for the open round."""

    template: Literal[PromptTemplate.ROUND] = PromptTemplate.ROUND
    round_number: int = Field(gt=0)
    retry: int = Field(ge=1)
    hypothesis_id: str
    title: str = ""
    hypothesis: str
    task: str
    pass_criteria: str
    invariants: str = ""
    activation_evidence: str = ""
    falsification_criteria: str = ""
    minimum_acceptance_criteria: str = ""
    base_revision: RevisionRef
    feedback: str | None = None
    no_valid_reply: str | None = None
    dominated_by_rounds: tuple[int, ...] = ()
    official_due: bool = False
    official_reason: str | None = None
    interface: str = "inprocess"
    profiler_id: str = "none"
    profiler_support: str | None = None


type PromptContext = Annotated[
    PlanPrompt | RoundPrompt | ResumePrompt | ReplyCorrectionPrompt | WaitUnrecordedPrompt,
    Field(discriminator="template"),
]
