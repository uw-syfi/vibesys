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
from vibesys.orchestration.dynamic.models import UnappliedPart
from vibesys.orchestration.dynamic.search import _roles as roles
from vibesys.orchestration.dynamic.search._queue import PickPolicy
from vibesys.orchestration.dynamic.search._values import (
    Blocker,
    CombinePick,
    CombineSource,
    DropReason,
    Failure,
    MainMeasurement,
    MergeRecord,
    MergeSource,
    PlanProblem,
    WorkKind,
)
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
    OUTLINE = "outline"
    MERGE = "merge"


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


class MergeableRow(Value):
    """A buildable candidate that passes accuracy and beats main's latest measurement."""

    hypothesis_id: str
    revision: RevisionRef
    metric: MetricRow | None = None


class PartsVerdict(StrEnum):
    """How a composite measured against the best of its parts. Informational: it adopts nothing."""

    BETTER = "better"
    WITHIN_NOISE = "within_noise"
    WORSE = "worse"
    UNKNOWN = "unknown"
    """The composite, or every part, has no trusted headline to compare."""


class CompositeRow(Value):
    """A combine's candidate beside its parts.

    Each part carries its trusted headline; the verdict compares the composite's with the best.
    """

    parts: tuple[CombineSource, ...]
    verdict: PartsVerdict
    unapplied: tuple[UnappliedPart, ...] = ()
    """What the implementer said it could not apply."""


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
    composite: CompositeRow | None = None
    """For a hypothesis a combine started: its parts and how it compares with them."""


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


class QueuedEvaluation(Value):
    """A trusted measurement a workstream asked for: holding a slot, or waiting for one."""

    work_id: str
    kind: WorkKind
    revision: RevisionRef
    waited_seconds: int | None = None
    """Seconds since it was submitted; None when the request was built without the clock."""


class DroppedEvaluation(Value):
    """A candidate dropped from the queue without being measured."""

    work_id: str
    revision: RevisionRef
    reason: DropReason
    superseded_by: str | None = None


class EvaluationQueueView(Value):
    """The run's trusted evaluations as the orchestrator sees them."""

    pick: PickPolicy
    slots: int = Field(gt=0)
    input_holds_slot: bool
    """The input's own measurement holds a slot (it always goes first)."""
    measuring: tuple[QueuedEvaluation, ...] = ()
    waiting: tuple[QueuedEvaluation, ...] = ()
    """In the order free slots will take them."""
    dropped: tuple[DroppedEvaluation, ...] = ()
    """The latest candidates dropped unmeasured."""
    ranking: tuple[str, ...] = ()
    """The order the orchestrator last gave (``ranked`` only)."""


class InputOverdueView(Value):
    """The input's measurement has kept a workstream waiting longer than the run allows."""

    waited_seconds: int | None = None
    """How long the first workstream has waited; None when the request was built without the
    clock."""
    limit_seconds: int


class _Planning(RolePromptContext):
    """What a planning request says; the request and its correction share it."""

    reply_schema: ClassVar[SchemaRef | None] = roles.PLANNER.reply_schema

    capacity: int = Field(ge=0)
    in_flight: int = Field(ge=0)
    remaining: int = Field(ge=0)
    base_revision: RevisionRef
    base_accuracy_passed: bool | None
    offer_snapshot: str
    main_revision: RevisionRef
    """The run's main: where a new workstream that names no parent starts."""
    main_measurement: MainMeasurement | None = None
    """The latest trusted measurement of a merged main; None while main is the input."""
    merge_running: str | None = None
    """The merge under way, if one is: main moves when it finishes."""
    mergeable: tuple[MergeableRow, ...] = ()
    merges: tuple[MergeRecord, ...] = ()
    """Every finished merge, oldest first."""
    buildable: tuple[BuildableRow, ...] = ()
    history: tuple[WorkstreamHistoryRow, ...] = ()
    older_ids: tuple[str, ...] = ()
    baseline: tuple[MetricRow, ...] = ()
    input_failure: InputFailure | None = None
    baseline_pending: bool = False
    """The input is still being measured: what is scheduled now starts when its reading lands."""
    profiling: bool = False
    profile_fields: tuple[str, ...] = ()
    """The measurement fields a profile workstream may require: what the capture supplies."""
    handoff_paths: tuple[str, ...] = ()
    """Where the earlier planning calls' handoff files are, newest first: paths, never contents."""
    evaluations: EvaluationQueueView | None = None
    """The trusted evaluation queue; None in a request built before the queue existed."""
    input_overdue: InputOverdueView | None = None
    """The input's measurement is overdue: workstreams wait for it to settle their candidates."""
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
    baseline_pending: bool = False
    """The input's own measurement had not landed when the turn was asked."""
    input_failure: InputFailure | None = None
    """Why the input's own measurement failed, when it had when the turn was asked."""
    rebase_onto: RevisionRef | None = None
    """The run's main, when it advanced past this hypothesis's work: rebase onto it first."""
    combine: tuple[CombineSource, ...] = ()
    """For a combine: the parts to apply onto ``parent_revision`` (main), in order."""
    combine_missing: tuple[CombinePick, ...] = ()
    """For a combine: the parts the planner named that are no longer buildable, left out."""
    watch_for: str = ""
    """For a combine: what the planner said to check or be careful of."""


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


class OutlinePrompt(RolePromptContext):
    """Request to outline the steps of the task's plan from its documents."""

    reply_schema: ClassVar[SchemaRef | None] = roles.OUTLINER.reply_schema

    template: Literal[PromptTemplate.OUTLINE] = PromptTemplate.OUTLINE
    documents: tuple[str, ...]
    """The plan's documents, as paths relative to the task directory."""


class MergePrompt(RolePromptContext):
    """One merge of candidates into the run's main revision, on a checkout of main."""

    reply_schema: ClassVar[SchemaRef | None] = roles.MERGER.reply_schema

    template: Literal[PromptTemplate.MERGE] = PromptTemplate.MERGE
    merge_id: str
    main_revision: RevisionRef
    candidates: tuple[MergeSource, ...]
    instructions: str
    watch_for: str


type PromptContext = Annotated[
    PlannerPrompt
    | PlannerCorrection
    | ImplementPrompt
    | ReviewPrompt
    | ProfilePrompt
    | OutlinePrompt
    | MergePrompt,
    Field(discriminator="template"),
]
"""The union the render operation carries; the exchanges core starts are core's own contexts."""
