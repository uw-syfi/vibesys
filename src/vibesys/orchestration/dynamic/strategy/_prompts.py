"""Typed data the strategy hands to the declared prompt-rendering operation.

The strategy never builds prompt text. It sends a `PromptContext`; the operation's
executor renders the named template with `vs_prompts` and stores the artifacts.
Each context mirrors the variables of one existing dynamic template.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from vs_core.api import EvidenceId, RevisionRef, Value

from ._rows import MetricRow, PartialRow


class PromptTemplate(StrEnum):
    """Closed set of templates the strategy can request."""

    PORTFOLIO = "portfolio"
    PORTFOLIO_CORRECTION = "portfolio_correction"
    IMPLEMENT = "implement"
    REVIEW = "review"
    PROFILE_REQUEST = "profile_request"
    RESUME = "resume"
    REPLY_CORRECTION = "reply_correction"


class EvidenceCitation(Value):
    """An agent-cited measurement, bound to the revision it measured."""

    location: str
    revision: str | None = None
    purpose: str = ""


class BuildableRow(Value):
    """One exact retained, accuracy-verified revision offered as a parent."""

    option_id: str
    hypothesis_id: str
    revision: RevisionRef
    accuracy_evidence: EvidenceId
    benchmark_evidence: EvidenceId | None = None
    submission_index: int = Field(ge=0)
    latest_verified: bool
    best_partial: bool
    metric: MetricRow | None = None
    partial: PartialRow | None = None


class HistoryRow(Value):
    """Compact history of one hypothesis shown to the planner."""

    hypothesis_id: str
    sequence: int = Field(gt=0)
    title: str
    status: str
    strategy: str
    outcome: str | None = None
    summary: str = ""
    review_passed: bool | None = None
    accuracy_passed: bool | None = None
    benchmark_passed: bool | None = None
    candidate: RevisionRef | None = None
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None


class PlannerPrompt(Value):
    """Free-slot planning request."""

    template: Literal[PromptTemplate.PORTFOLIO] = PromptTemplate.PORTFOLIO
    capacity: int = Field(ge=0)
    in_flight: int = Field(ge=0)
    remaining: int = Field(ge=0)
    base_revision: RevisionRef
    base_accuracy_passed: bool | None
    offer_snapshot: str
    buildable: tuple[BuildableRow, ...] = ()
    history: tuple[HistoryRow, ...] = ()
    older_ids: tuple[str, ...] = ()
    baseline: tuple[MetricRow, ...] = ()
    input_failure: str | None = None
    profiling: bool = False


class PlannerCorrectionPrompt(Value):
    """The planning request again, with why the previous plan was not accepted."""

    template: Literal[PromptTemplate.PORTFOLIO_CORRECTION] = PromptTemplate.PORTFOLIO_CORRECTION
    planner: PlannerPrompt
    error: str | None
    scheduled: int = Field(ge=0)


class ImplementPrompt(Value):
    """One isolated hypothesis implementation request."""

    template: Literal[PromptTemplate.IMPLEMENT] = PromptTemplate.IMPLEMENT
    hypothesis_id: str
    hypothesis: str
    task: str
    pass_criteria: str
    parent_revision: RevisionRef
    evidence: tuple[EvidenceCitation, ...] = ()
    worktree_revision: RevisionRef | None = None
    prior_revision: RevisionRef | None = None
    feedback: str | None = None


class ReviewPrompt(Value):
    """Independent review of one exact candidate snapshot."""

    template: Literal[PromptTemplate.REVIEW] = PromptTemplate.REVIEW
    hypothesis_id: str
    hypothesis: str
    pass_criteria: str
    candidate: RevisionRef
    summary: str
    evidence: tuple[EvidenceCitation, ...] = ()


class ProfilePrompt(Value):
    """Measurement-only request to the profiler of one existing revision."""

    template: Literal[PromptTemplate.PROFILE_REQUEST] = PromptTemplate.PROFILE_REQUEST
    question: str
    required_fields: tuple[str, ...] = ()
    target: RevisionRef


class ResumePrompt(Value):
    """Continuation of a suspended turn after its awaited evaluations settled."""

    template: Literal[PromptTemplate.RESUME] = PromptTemplate.RESUME
    role: Literal["implementer", "judge", "profiler"]
    retained_revision: RevisionRef | None
    timed_out: bool
    evidence: tuple[EvidenceId, ...] = ()
    repeated_failure: str | None = None


class ReplyCorrectionPrompt(Value):
    """Why the previous reply violated its output schema."""

    template: Literal[PromptTemplate.REPLY_CORRECTION] = PromptTemplate.REPLY_CORRECTION
    role: Literal["planner", "implementer", "judge", "profiler"]
    error: str


type PromptContext = Annotated[
    PlannerPrompt
    | PlannerCorrectionPrompt
    | ImplementPrompt
    | ReviewPrompt
    | ProfilePrompt
    | ResumePrompt
    | ReplyCorrectionPrompt,
    Field(discriminator="template"),
]
