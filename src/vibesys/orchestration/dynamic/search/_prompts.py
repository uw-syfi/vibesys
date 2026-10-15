"""Typed data the search hands to the prompt renderer.

The search never builds prompt text. It sends a `PromptContext`; the render operation renders the
role's template with `vs_prompts` and stores the artifacts. Each context mirrors the variables of
one dynamic template. The contexts of the exchanges core starts (a reply that broke its schema, a
resumed turn, a wait nobody recorded) are core's, rendered by the role's `EXCHANGES` templates.
"""

from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from vibesys.candidates.api import InputFailure, ReviewEvaluation
from vibesys.metrics import MetricRow, PartialRow
from vibesys.orchestration.dynamic.search import _roles as roles
from vibesys.orchestration.dynamic.search._values import Blocker, Failure, PlanProblem
from vs_core.api.search import (
    Correction,
    EvidenceId,
    RevisionRef,
    RolePromptContext,
    SchemaRef,
    Value,
)


class PromptTemplate(StrEnum):
    """Closed set of templates the search can request."""

    PORTFOLIO = "portfolio"
    PORTFOLIO_CORRECTION = "portfolio_correction"
    IMPLEMENT = "implement"
    REVIEW = "review"
    PROFILE_REQUEST = "profile_request"


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
    failure_tail: str = ""
    failure: Failure | None = None
    """Why the workstream ended, unless a trusted check's output already says it."""
    failure_fence: str = "```"
    """The Markdown fence that holds the tail: longer than any backtick run inside it."""


class ProfileHistoryStatus(StrEnum):
    """What the trusted profile attempt established for its target."""

    RUNNING = "running"
    OBSERVED = "observed"
    FAILED = "failed"


class ProfileHistoryRow(Value):
    """One planned profile and the trusted evidence it produced."""

    kind: Literal["profile"] = "profile"
    profile_id: str
    sequence: int = Field(gt=0)
    target_hypothesis_id: str | None = None
    revision: RevisionRef
    question: str
    decision_impact: str
    status: ProfileHistoryStatus
    evidence_ids: tuple[EvidenceId, ...] = ()
    captured_fields: tuple[str, ...] = ()
    """What an observed profile established, as typed values: the fields it measured,"""
    metrics: tuple[MetricRow, ...] = ()
    """its decoded statistics (GPU-active fraction, top HIP API calls by time),"""
    artifacts: tuple[str, ...] = ()
    """and the artifacts it kept. None of it is cut; only the raw ``diagnosis`` text is."""
    diagnosis: str = ""
    """The tail of the capture's printed output, where its attribution tables end."""
    failure: Failure | None = None
    """Why a profile failed; None unless ``status`` is failed."""
    failure_output: str = ""
    """What the failed profile's evidence printed, when it printed any."""
    detail_fence: str = "```"


type WorkstreamHistoryRow = HistoryRow | ProfileHistoryRow


class _Planning(RolePromptContext):
    """What a planning request says; the request and its correction share it."""

    reply_schema: ClassVar[SchemaRef | None] = roles.PLANNER.reply_schema

    capacity: int = Field(ge=0)
    in_flight: int = Field(ge=0)
    remaining: int = Field(ge=0)
    base_revision: RevisionRef
    base_accuracy_passed: bool | None
    offer_snapshot: str
    buildable: tuple[BuildableRow, ...] = ()
    history: tuple[WorkstreamHistoryRow, ...] = ()
    older_ids: tuple[str, ...] = ()
    baseline: tuple[MetricRow, ...] = ()
    input_failure: InputFailure | None = None
    profiling: bool = False
    profile_fields: tuple[str, ...] = ()
    """The measurement fields a profile workstream may require: what the capture supplies."""
    handoff_paths: tuple[str, ...] = ()
    """Where the earlier planning calls' handoff files are, newest first: paths, never contents."""
    problem: PlanProblem | None = None
    """Why the earlier reply, or the earlier planning call, was not accepted; None if it was lost."""


class PlannerPrompt(_Planning):
    """Free-slot planning request, in a new turn."""

    template: Literal[PromptTemplate.PORTFOLIO] = PromptTemplate.PORTFOLIO


class PlannerCorrection(_Planning, Correction):
    """The planning request again, as the correction of the earlier turn that failed.

    ``problem`` says why that reply was not accepted; None when the turn was lost and the same
    question is simply asked again.
    """

    template: Literal[PromptTemplate.PORTFOLIO_CORRECTION] = PromptTemplate.PORTFOLIO_CORRECTION


class ImplementPrompt(RolePromptContext):
    """One isolated hypothesis implementation request."""

    reply_schema: ClassVar[SchemaRef | None] = roles.IMPLEMENTER.reply_schema

    template: Literal[PromptTemplate.IMPLEMENT] = PromptTemplate.IMPLEMENT
    hypothesis_id: str
    hypothesis: str
    task: str
    pass_criteria: str
    parent_revision: RevisionRef
    evidence: tuple[EvidenceCitation, ...] = ()
    worktree_revision: RevisionRef | None = None
    prior_revision: RevisionRef | None = None
    rejection: str | None = None
    """The reviewer's feedback on the candidate the previous turn produced."""
    prior_failure_tail: str = ""
    prior_failure_fence: str = "```"
    last_blocker: Blocker | None = None
    """The latest turn of this workstream that ended without a candidate."""
    turns_without_candidate: int = Field(default=0, ge=0)


class ReviewPrompt(RolePromptContext):
    """Independent review of one exact candidate snapshot."""

    reply_schema: ClassVar[SchemaRef | None] = roles.JUDGE.reply_schema

    template: Literal[PromptTemplate.REVIEW] = PromptTemplate.REVIEW
    hypothesis_id: str
    hypothesis: str
    pass_criteria: str
    candidate: RevisionRef
    summary: str
    evidence: tuple[EvidenceCitation, ...] = ()
    evaluations: tuple[ReviewEvaluation, ...] = ()
    """Every trusted evaluation of ``candidate``, oldest first; empty when none exists yet."""


class ProfilePrompt(RolePromptContext):
    """Measurement-only request to the profiler of one existing revision."""

    reply_schema: ClassVar[SchemaRef | None] = roles.PROFILER.reply_schema

    template: Literal[PromptTemplate.PROFILE_REQUEST] = PromptTemplate.PROFILE_REQUEST
    question: str
    required_fields: tuple[str, ...] = ()
    target: RevisionRef


type PromptContext = Annotated[
    PlannerPrompt | PlannerCorrection | ImplementPrompt | ReviewPrompt | ProfilePrompt,
    Field(discriminator="template"),
]
"""The union the render operation carries; the exchanges core starts are core's own contexts."""
