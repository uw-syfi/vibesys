"""Typed data the strategy hands to the declared prompt-rendering operation.

The strategy never builds prompt text. It sends a `PromptContext`; the operation's executor
renders the named template with `vs_prompts` and stores the artifacts. The evidence the legacy
loop kept in progress files (round history, the Pareto frontier, carried notices, the profile,
the implementer's claims for the judge) is rendered from the typed rows below, because a
round's workspace is an isolated child of a revision and holds no progress files.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from vibesys.prompts import ReplyCorrectionPrompt, ResumePrompt, WaitUnrecordedPrompt
from vibesys.serial_search.api import PlanEvidence
from vs_core.api import EvidenceRef, RevisionRef, RolePromptContext, SchemaRef, Value

from ._schemas import IMPLEMENTER_REPLY, JUDGE_REPLY, PLANNER_REPLY, PRE_ROUND_REPLY, PROFILER_REPLY


class PromptTemplate(StrEnum):
    """Closed set of multi's own templates (the shared protocol ones are `CoreProtocolTemplate`)."""

    PRE_ROUND = "pre_round"
    PROFILER = "profiler"
    PLAN = "plan"
    IMPLEMENT = "implement"
    CONTINUE = "continue"
    JUDGE = "judge"


class PreRoundPrompt(RolePromptContext):
    """The routing decision before a new hypothesis: is fresh profile evidence wanted?"""

    reply_schema: ClassVar[SchemaRef | None] = PRE_ROUND_REPLY

    template: Literal[PromptTemplate.PRE_ROUND] = PromptTemplate.PRE_ROUND
    round_number: int = Field(gt=0)
    has_history: bool
    profiler_kind: str
    profile_execution: str
    regression_kind: str | None = None
    regression_round: int | None = None
    exhaustion_feedback: str | None = None
    exhaustion_round: int | None = None


class ProfilerPrompt(RolePromptContext):
    """One advisory profiling turn: where to look and where to write bounded evidence."""

    reply_schema: ClassVar[SchemaRef | None] = PROFILER_REPLY

    template: Literal[PromptTemplate.PROFILER] = PromptTemplate.PROFILER
    round_number: int = Field(gt=0)
    profiler_kind: str
    profile_focus: str
    benchmark_command: str | None = None
    profile_execution: str
    profiler_support_name: str
    profiler_mcp_name: str
    evidence_directory: str


class PlanPrompt(PlanEvidence, RolePromptContext):
    """One new-hypothesis planning request."""

    reply_schema: ClassVar[SchemaRef | None] = PLANNER_REPLY

    template: Literal[PromptTemplate.PLAN] = PromptTemplate.PLAN
    base_revision: RevisionRef


class Claim(Value):
    """One named number the implementer reported."""

    name: str
    value: float


class ImplementerClaims(Value):
    """What the implementer reported, as untrusted data for the judge."""

    summary: str
    expected_behavior: str
    hypothesis_outcome: str
    evidence: str = ""
    next_step: str = ""
    perf_metric: float | None = None
    perf_unit: str | None = None
    metrics: tuple[Claim, ...] = ()
    evaluation_artifact: str | None = None
    candidate_disposition: str
    candidate_metrics: tuple[Claim, ...] = ()
    candidate_evaluation_artifact: str | None = None
    candidate_operating_point: str = ""
    candidate_retention_reason: str = ""
    validation_recipe_artifact: str | None = None


class ConflictMetric(Value):
    """One objective value of a point on the live archive."""

    name: str
    value: float
    direction: Literal["max", "min"]


class Dominator(Value):
    """A trusted archive point that dominates a claimed frontier row."""

    round_number: int = Field(gt=0)
    metrics: tuple[ConflictMetric, ...] = ()


class ArchiveConflictView(Value):
    """A `pareto_frontier` claim that the live archive dominates."""

    dominators: tuple[Dominator, ...] = Field(min_length=1)


class ValidationFailure(Value):
    """How the trusted run of the implementer's validation recipes ended, when it did not pass."""

    recipe: str = ""
    error: str = ""
    output: str = ""
    recipe_unusable: bool = False


class HypothesisPlan(Value):
    """The designer's typed plan of the hypothesis a round works on."""

    hypothesis_id: str
    title: str = ""
    hypothesis: str
    task: str
    pass_criteria: str
    invariants: str = ""
    activation_evidence: str = ""
    falsification_criteria: str = ""
    minimum_acceptance_criteria: str = ""


class GateFacts(Value):
    """What the framework's gates and parent choice already established for the hypothesis."""

    revert_applied: bool = False
    revert_round: int | None = None
    revert_commit: str | None = None
    revalidation_pending: bool = False
    approved_evaluation_artifact: str | None = None
    approved_perf_metric: float | None = None
    approved_perf_unit: str | None = None


class ImplementPrompt(RolePromptContext):
    """The first implementation request of a hypothesis, or a retry of it."""

    reply_schema: ClassVar[SchemaRef | None] = IMPLEMENTER_REPLY

    template: Literal[PromptTemplate.IMPLEMENT] = PromptTemplate.IMPLEMENT
    round_number: int = Field(gt=0)
    retry: int = Field(ge=1)
    plan: HypothesisPlan
    base_revision: RevisionRef
    framework: GateFacts
    feedback: str | None = None
    validation_failure: ValidationFailure | None = None
    no_valid_reply: str | None = None
    pareto_archive_conflict: ArchiveConflictView | None = None
    official_due: bool = False
    official_reason: str | None = None
    benchmark_enabled: bool = True
    active_component: str | None = None
    reference_path: str = "."
    interface: str = "inprocess"


class ContinuePrompt(RolePromptContext):
    """A bounded continuation of a hypothesis the implementer asked to keep working on."""

    reply_schema: ClassVar[SchemaRef | None] = IMPLEMENTER_REPLY

    template: Literal[PromptTemplate.CONTINUE] = PromptTemplate.CONTINUE
    round_number: int = Field(gt=0)
    retry: int = Field(ge=1)
    plan: HypothesisPlan
    base_revision: RevisionRef
    continuation_step: str
    framework: GateFacts
    feedback: str | None = None
    validation_failure: ValidationFailure | None = None
    no_valid_reply: str | None = None
    pareto_archive_conflict: ArchiveConflictView | None = None


class JudgePrompt(RolePromptContext):
    """The independent review of one implementer reply."""

    reply_schema: ClassVar[SchemaRef | None] = JUDGE_REPLY
    described_replies: ClassVar[Mapping[str, SchemaRef]] = MappingProxyType(
        {"implementer_reply": IMPLEMENTER_REPLY}
    )

    template: Literal[PromptTemplate.JUDGE] = PromptTemplate.JUDGE
    round_number: int = Field(gt=0)
    retry: int = Field(ge=1)
    plan: HypothesisPlan
    claims: ImplementerClaims
    framework: GateFacts
    pareto_archive_conflict: ArchiveConflictView | None = None
    official_due: bool = False
    official_reason: str | None = None
    benchmark_enabled: bool = True
    interface: str = "inprocess"
    # The trusted evaluations the candidate was measured by, stated at render time.
    cited_evidence: tuple[EvidenceRef, ...] = ()


type PromptContext = Annotated[
    PreRoundPrompt
    | ProfilerPrompt
    | PlanPrompt
    | ImplementPrompt
    | ContinuePrompt
    | JudgePrompt
    | ResumePrompt
    | ReplyCorrectionPrompt
    | WaitUnrecordedPrompt,
    Field(discriminator="template"),
]
