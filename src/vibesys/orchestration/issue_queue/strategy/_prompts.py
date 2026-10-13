"""Typed data the strategy hands to the declared prompt-rendering operation.

The strategy never builds prompt text. It sends a `PromptContext`; the operation's executor
renders the named template with `vs_prompts` and stores the artifacts. Each context mirrors
the variables of one template. The board is part of the data: agents on the core lifecycle
work in an isolated checkout, so they read the issue under work in full and every other
issue as a list from the prompt, not from files.
"""

from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import ConfigDict, Field

from vs_core.api import EvidenceRef, RevisionRef, RolePromptContext, SchemaRef, Value

from ._schemas import IMPLEMENTER_REPLY, JUDGE_REPLY, PERF_REPLY


class PromptTemplate(StrEnum):
    """Closed set of templates the strategy can request."""

    IMPLEMENT = "implement"
    REVIEW = "review"
    PERFORMANCE = "performance"
    RESUME = "resume"
    REPLY_CORRECTION = "reply_correction"
    WAIT_UNRECORDED = "wait_unrecorded"


class IssueBrief(Value):
    """One issue as the list the agents see."""

    # Fields declared with this class hold exactly this class (a subclass instance is rebuilt as
    # one on validation), so one remembered JSON text per instance holds wherever it sits.
    model_config = ConfigDict(revalidate_instances="subclass-instances")

    id: int = Field(ge=1)
    type: str
    status: str
    title: str
    attempts: int = Field(ge=0)


class IssueDetail(IssueBrief):
    """The issue under work, in full."""

    description: str


class ImplementPrompt(RolePromptContext):
    """One bounded implementation attempt of one issue."""

    reply_schema: ClassVar[SchemaRef | None] = IMPLEMENTER_REPLY

    template: Literal[PromptTemplate.IMPLEMENT] = PromptTemplate.IMPLEMENT
    issue: IssueDetail
    board: tuple[IssueBrief, ...] = ()
    # The judge's feedback and analysis when the previous attempt at this issue failed review.
    feedback: str | None = None
    analysis: str | None = None


class ReviewPrompt(RolePromptContext):
    """Independent review of one issue's candidate."""

    reply_schema: ClassVar[SchemaRef | None] = JUDGE_REPLY

    template: Literal[PromptTemplate.REVIEW] = PromptTemplate.REVIEW
    issue: IssueDetail
    board: tuple[IssueBrief, ...] = ()
    summary: str
    files_touched: tuple[str, ...] = ()
    self_check: str = ""
    # The trusted evaluations this issue's candidate was measured by, stated at render time.
    cited_evidence: tuple[EvidenceRef, ...] = ()


class PriorPerformance(Value):
    """One finished performance evaluation, shown to the next one."""

    iteration: int = Field(gt=0)
    throughput_trend: str
    latency_trend: str
    metrics_json: str
    new_issues: int = Field(ge=0)


class PerformancePrompt(RolePromptContext):
    """One measured performance assessment and the issues it may file."""

    reply_schema: ClassVar[SchemaRef | None] = PERF_REPLY

    template: Literal[PromptTemplate.PERFORMANCE] = PromptTemplate.PERFORMANCE
    iteration: int = Field(gt=0)
    max_new_issues: int = Field(gt=0)
    board: tuple[IssueBrief, ...] = ()
    prior: tuple[PriorPerformance, ...] = ()
    load_levels_json: str | None = None


class ResumePrompt(Value):
    """Continuation of a suspended turn after its awaited evaluations settled."""

    template: Literal[PromptTemplate.RESUME] = PromptTemplate.RESUME
    role: Literal["implementer", "judge", "perf-evaluator"]
    retained_revision: RevisionRef | None
    timed_out: bool
    cited_evidence: tuple[EvidenceRef, ...] = ()
    repeated_failure: str | None = None


class ReplyCorrectionPrompt(Value):
    """Why the previous reply violated its output schema."""

    template: Literal[PromptTemplate.REPLY_CORRECTION] = PromptTemplate.REPLY_CORRECTION
    role: Literal["implementer", "judge", "perf-evaluator"]
    error: str


class WaitUnrecordedPrompt(Value):
    """The previous reply said it was waiting for an evaluation, but the run recorded no wait."""

    template: Literal[PromptTemplate.WAIT_UNRECORDED] = PromptTemplate.WAIT_UNRECORDED


type PromptContext = Annotated[
    ImplementPrompt
    | ReviewPrompt
    | PerformancePrompt
    | ResumePrompt
    | ReplyCorrectionPrompt
    | WaitUnrecordedPrompt,
    Field(discriminator="template"),
]
