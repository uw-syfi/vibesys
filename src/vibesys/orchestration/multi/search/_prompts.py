"""Typed data the multi search hands to the prompt renderer.

The search never builds prompt text. It sends a `PromptContext`; the render operation renders
the role's template and stores the artifacts. The designer's requests are shared with the single
search. What the legacy loop kept in progress files (round history, the Pareto frontier, carried
notices, the profile, the implementer's claims for the judge) is rendered from typed rows,
because a round's workspace is an isolated child of a revision and holds no progress files. What
is fixed for the whole run (the profiler's kind and tool server, the benchmark command, the
reference location, the interface) is a run-level prompt variable the host supplies, not a field
here. The contexts of the exchanges core starts (a reply that broke its schema, a resumed turn,
a wait nobody recorded) are core's, rendered by the role's `EXCHANGES` templates.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from vibesys.orchestration.multi.search._roles import IMPLEMENTER, JUDGE, PROFILER, ROUTER
from vibesys.serial_search.api import PlanCorrection, PlanPrompt
from vs_core.api.search import EvidenceRef, RevisionRef, RolePromptContext, SchemaRef, Value


class PromptTemplate(StrEnum):
    """Closed set of multi's own contexts: the designer's are `PlanTemplate`."""

    PRE_ROUND = "pre_round"
    PROFILER = "profiler"
    IMPLEMENT = "implement"
    JUDGE = "judge"


_GIVEN_ONLY = frozenset({"base_revision", "base_round", "base_value"})
"""Router prompt fields written only when given: later additions to a recorded shape."""


class PreRoundPrompt(RolePromptContext):
    """The routing decision before a new hypothesis: is fresh profile evidence wanted?"""

    reply_schema: ClassVar[SchemaRef | None] = ROUTER.reply_schema

    template: Literal[PromptTemplate.PRE_ROUND] = PromptTemplate.PRE_ROUND
    round_number: int = Field(gt=0)
    # These three fields were added after runs had recorded router prompts, which hold them
    # absent, as null, or set. Each is written only when the prompt was given it, so every
    # recorded prompt re-encodes to the document that was saved: recovery proves each recorded
    # request against the digest of that document (`_written_as_given`).
    base_revision: RevisionRef | None = None
    """The round's base: the best revision so far, the input while none beat it.

    None only in prompts saved before routers were shown the base; every new round sets it.
    """
    base_round: int | None = None
    """The round whose idea made ``base_revision``; None when it is the input."""
    base_value: float | None = None
    """The headline the trusted gates measured of ``base_revision``; None when they have not."""
    has_history: bool
    regression_kind: str | None = None
    regression_round: int | None = None
    exhaustion_feedback: str | None = None
    exhaustion_round: int | None = None

    @model_serializer(mode="wrap")
    def _written_as_given(self, write: SerializerFunctionWrapHandler) -> dict[str, object]:
        """The prompt as written, without a later-added field the prompt was not given."""
        written = write(self)
        for name in _GIVEN_ONLY - self.model_fields_set:
            written.pop(name, None)
        return written


class ProfilerPrompt(RolePromptContext):
    """One advisory profiling turn: where to look."""

    reply_schema: ClassVar[SchemaRef | None] = PROFILER.reply_schema

    template: Literal[PromptTemplate.PROFILER] = PromptTemplate.PROFILER
    round_number: int = Field(gt=0)
    profile_focus: str


class Claim(Value):
    """One named number the implementer reported."""

    name: str
    value: float


class ImplementerClaims(Value):
    """What the implementer reported, as untrusted data for the judge."""

    summary: str
    expected_behavior: str
    status: str
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
    minimum_ratio: float = 1.0


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
    """The first implementation request of a hypothesis, a retry of it, or a continuation.

    A hypothesis the implementer asked to keep working on is continued with ``continuation_step``,
    the bounded delta it named; the template then asks for that delta instead of the whole plan.
    """

    reply_schema: ClassVar[SchemaRef | None] = IMPLEMENTER.reply_schema

    template: Literal[PromptTemplate.IMPLEMENT] = PromptTemplate.IMPLEMENT
    round_number: int = Field(gt=0)
    retry: int = Field(ge=1)
    plan: HypothesisPlan
    base_revision: RevisionRef
    framework: GateFacts
    continuation_step: str | None = None
    feedback: str | None = None
    validation_failure: ValidationFailure | None = None
    no_valid_reply: str | None = None
    pareto_archive_conflict: ArchiveConflictView | None = None
    benchmark_enabled: bool = True
    active_component: str | None = None


class JudgePrompt(RolePromptContext):
    """The independent review of one implementer reply."""

    reply_schema: ClassVar[SchemaRef | None] = JUDGE.reply_schema
    described_replies: ClassVar[Mapping[str, SchemaRef]] = MappingProxyType(
        {"implementer_reply": IMPLEMENTER.reply_schema}
    )

    template: Literal[PromptTemplate.JUDGE] = PromptTemplate.JUDGE
    round_number: int = Field(gt=0)
    retry: int = Field(ge=1)
    plan: HypothesisPlan
    claims: ImplementerClaims
    framework: GateFacts
    pareto_archive_conflict: ArchiveConflictView | None = None
    benchmark_enabled: bool = True
    # The trusted evaluations the candidate was measured by, stated at render time.
    cited_evidence: tuple[EvidenceRef, ...] = ()


type PromptContext = Annotated[
    PlanPrompt | PlanCorrection | PreRoundPrompt | ProfilerPrompt | ImplementPrompt | JudgePrompt,
    Field(discriminator="template"),
]
"""The union the render operation carries; the exchanges core starts are core's own contexts."""
