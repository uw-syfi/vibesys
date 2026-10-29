"""Strict contracts and durable state for dynamic hypothesis portfolios."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast, override

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
)
from pydantic.json_schema import GenerateJsonSchema

from vibesys.hypothesis import DecidedFrom, HypothesisOutcome
from vibesys.hypothesis.plan import HypothesisStrategyUpdate
from vibesys.inputs import MAX_STEPS, PlanStep, outline_problems
from vs_core.api import RevisionReply, Value
from vs_runtime.api import (
    AgentId,
    ProfileField,
)

if TYPE_CHECKING:
    from pydantic.json_schema import JsonSchemaMode, JsonSchemaValue
    from pydantic_core import core_schema


# Agent free-text fields carry no length cap. An agent cannot count characters:
# under Claude an over-long value fails the schema, and under Codex the output
# schema constrains decoding, so a capped string is cut off mid-word and the
# turn still succeeds. Every consumer that renders a field into a bounded view
# truncates there (see ``rounds``); ``EvidenceReference.purpose`` is the one
# capped text, wide enough that a sentence never reaches it.
class EvidenceAttributionError(ValueError):
    """An agent citation names a revision different from its host-owned measurement."""


class EvidenceReference(BaseModel):
    """Compact pointer to evidence stored outside agent conversation history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str = Field(min_length=1, max_length=512)
    purpose: str = Field(min_length=1, max_length=1000)
    revision: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("location", "purpose", "revision")
    @classmethod
    def _nonblank_reference(cls, value: str | None) -> str | None:
        """Require meaningful evidence pointers without rewriting cited paths or text."""
        if value is not None and not value.strip():
            message = "evidence reference must not be blank"
            raise ValueError(message)
        return value

    def with_revision(self, revision: str) -> EvidenceReference:
        """Bind an immutable measured revision, rejecting conflicting attribution."""
        if self.revision is not None and self.revision != revision:
            message = (
                f"evidence {self.location!r}.revision: {self.revision!r} does not match "
                f"measured revision {revision!r}"
            )
            raise EvidenceAttributionError(message)
        return self.model_copy(update={"revision": revision})


class WorkstreamKind(StrEnum):
    """What a scheduled workstream does with its slot."""

    # Implement a hypothesis in a candidate workspace (the default).
    IMPLEMENT = "implement"
    # Profile an existing candidate revision to inform later plans.
    PROFILE = "profile"
    # Merge candidates into the run's main revision.
    MERGE = "merge"
    # Build one new candidate, from main, out of several retained candidates.
    COMBINE = "combine"


class WorkstreamPlan(BaseModel):
    """Implement a hypothesis, including features or fixes, producing reviewable work.

    Declaring a new hypothesis is part of this workstream, not a separate
    planning action. Use this kind whenever the slot must change code.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[WorkstreamKind.IMPLEMENT] = Field(
        default=WorkstreamKind.IMPLEMENT,
        description="Use implement when the slot must change code or produce a candidate.",
    )
    hypothesis_id: AgentId = Field(description="The hypothesis this workstream implements.")
    title: str = Field(min_length=1, description="Name of the implementation goal.")
    hypothesis: str = Field(min_length=1, description="The claim this implementation will test.")
    task: str = Field(min_length=1, description="The concrete changes the implementer must make.")
    pass_criteria: str = Field(
        min_length=1, description="Observable evidence that the implementation meets its goal."
    )
    continue_hypothesis: bool = Field(
        default=False,
        description=(
            "False for a new hypothesis ID; true to continue the existing hypothesis "
            "that has this ID."
        ),
    )
    evidence: tuple[EvidenceReference, ...] = Field(
        default=(),
        max_length=8,
        description="Measurements this workstream rests on, cited by location and measured revision.",
    )
    parent_hypothesis_id: AgentId | None = Field(
        default=None,
        description=(
            "For a new hypothesis only: the ID of an existing hypothesis listed as a "
            "buildable candidate; the workstream starts from that candidate's revision. "
            "Null to start from the current base revision."
        ),
    )

    parent_revision: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        min_length=1,
        description=(
            "Exact offered revision of parent_hypothesis_id for a new sibling. "
            "Omitting it retains the legacy latest candidate selection."
        ),
    )

    @field_validator("title", "hypothesis", "task", "pass_criteria")
    @classmethod
    def _nonblank_intent(cls, value: str) -> str:
        """Require meaningful implementation intent without rewriting agent text."""
        if not value.strip():
            message = "implementation intent must not be blank"
            raise ValueError(message)
        return value


# A profile question is capped wide enough that a paragraph never reaches it,
# and below the profiler service's request limit.
MAX_PROFILE_QUESTION_CHARS = 4000


class ProfilePlan(BaseModel):
    """Measure an existing revision without editing it or producing a candidate.

    Historical durable plans may lack decision_impact; new planner decisions
    use ProfileDecision, which requires that intent explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[WorkstreamKind.PROFILE] = Field(
        description=(
            "Use profile only to measure an existing revision. It cannot implement a "
            "feature, fix correctness, change code, or produce a candidate."
        )
    )
    profile_id: AgentId = Field(
        description="A new ID for this profile, distinct from every hypothesis and profile ID."
    )
    target_hypothesis_id: AgentId | None = Field(
        description=(
            "The ID of a hypothesis listed as a buildable candidate, to profile its revision; "
            "null to profile the current base revision."
        ),
    )
    question: str = Field(
        min_length=1,
        max_length=MAX_PROFILE_QUESTION_CHARS,
        description="What to measure on this revision, not an implementation task or kind choice.",
    )
    required_fields: tuple[ProfileField, ...] = Field(
        default=(),
        description="Required measurement fields. Aggregate timing cannot answer phase or HIP API requirements.",
    )
    decision_impact: str | None = Field(
        default=None,
        description="Which next implementation decision this measurement will inform, and how.",
    )

    @field_validator("question", "decision_impact")
    @classmethod
    def _nonblank_intent(cls, value: str | None) -> str | None:
        """Retain historical absent intent, but reject blank measurement intent."""
        if value is not None and not value.strip():
            message = "measurement intent must not be blank"
            raise ValueError(message)
        return value


class ProfileDecision(ProfilePlan):
    """A measurement-only decision naming its effect on the next implementation plan."""

    decision_impact: str = Field(
        min_length=1,
        description=(
            "Why this measurement deserves a slot: state which next implementation decision "
            "depends on the answer and how different results would change that decision. "
            "If the slot must implement anything, use kind implement instead."
        ),
    )


MAX_MERGE_CANDIDATES = 8
"""The most candidates one merge may combine."""


class MergePlan(BaseModel):
    """Merge buildable candidates into the run's main revision, in the order given.

    A merge agent applies them on a checkout of main, resolves conflicts and runs the project's
    fast tests; its commit is then measured and becomes main when it passes the trusted accuracy
    check and measures no worse than main. It may not redesign:
    when the instructions cannot be followed it stops and says why.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[WorkstreamKind.MERGE] = Field(
        description="Use merge to combine buildable candidates into the run's main revision."
    )
    merge_id: AgentId = Field(
        description="A new ID for this merge, distinct from every hypothesis, profile and merge ID."
    )
    candidates: tuple[AgentId, ...] = Field(
        min_length=1,
        max_length=MAX_MERGE_CANDIDATES,
        description=(
            "Hypothesis IDs of listed buildable candidates, in the order to merge them; each "
            "is merged at its latest verified revision."
        ),
    )
    instructions: str = Field(
        min_length=1,
        description=(
            "How to merge them: which changes add up, which of two alternatives to take, which "
            "parts duplicate main and are dropped."
        ),
    )
    watch_for: str = Field(
        min_length=1,
        description="What the merger must check or be careful of while merging.",
    )

    @field_validator("instructions", "watch_for")
    @classmethod
    def _nonblank_intent(cls, value: str) -> str:
        """Require meaningful merge instructions without rewriting the planner's text."""
        if not value.strip():
            message = "merge instructions must not be blank"
            raise ValueError(message)
        return value


MAX_COMBINE_PARENTS = MAX_MERGE_CANDIDATES
"""The most retained candidates one combine may build its candidate from."""


class CombineParent(BaseModel):
    """One part of a combine: an exact offered revision of a buildable candidate."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: AgentId = Field(description="The ID of a hypothesis listed as buildable.")
    revision: str = Field(
        min_length=1, description="An exact revision of that hypothesis the buildable list offers."
    )


class CombinePlan(BaseModel):
    """Build one new candidate from several buildable candidates, starting from main.

    The implementer applies each part's own change on a checkout of main, in the order given,
    runs the project's fast tests and submits the result. The composite is a candidate of a new
    hypothesis, judged and measured like any other; it does not move main. A part may have lost
    or tied alone: a combine tests whether the parts work together.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[WorkstreamKind.COMBINE] = Field(
        description="Use combine to build one new candidate out of several buildable candidates."
    )
    hypothesis_id: AgentId = Field(
        description="A new hypothesis ID for the composite, unused by any hypothesis, profile or merge."
    )
    title: str = Field(min_length=1, description="Name of the combination.")
    hypothesis: str = Field(
        min_length=1, description="Why these parts should work better together than alone."
    )
    parents: tuple[CombineParent, ...] = Field(
        min_length=2,
        max_length=MAX_COMBINE_PARENTS,
        description=(
            "The parts, each an exact offered (hypothesis, revision) pair of a different "
            "hypothesis, in the order to apply them."
        ),
    )
    instructions: str = Field(
        min_length=1,
        description="How to combine the parts: which side wins a conflict, what to leave out.",
    )
    watch_for: str = Field(
        min_length=1, description="What the implementer must check or be careful of."
    )
    pass_criteria: str = Field(
        min_length=1, description="Observable evidence that the composite meets its goal."
    )

    @field_validator("title", "hypothesis", "instructions", "watch_for", "pass_criteria")
    @classmethod
    def _nonblank_intent(cls, value: str) -> str:
        """Require meaningful combination intent without rewriting the planner's text."""
        if not value.strip():
            message = "combine intent must not be blank"
            raise ValueError(message)
        return value


def _workstream_kind(value: object) -> str:
    """Return a planned workstream's kind; an entry without one is an implement workstream."""
    if isinstance(value, dict):
        return str(value.get("kind", WorkstreamKind.IMPLEMENT))
    return str(getattr(value, "kind", WorkstreamKind.IMPLEMENT))


# Validation dispatches on ``kind``, so an invalid entry is reported against
# its own kind's fields only, under ``workstreams.N.<kind>``. ``kind`` defaults
# to implement, as ``WorkstreamPlan`` declares, so plans written before profile
# workstreams existed still validate.
type PlannedWorkstream = Annotated[
    Annotated[WorkstreamPlan, Tag(WorkstreamKind.IMPLEMENT.value)]
    | Annotated[ProfilePlan, Tag(WorkstreamKind.PROFILE.value)]
    | Annotated[MergePlan, Tag(WorkstreamKind.MERGE.value)]
    | Annotated[CombinePlan, Tag(WorkstreamKind.COMBINE.value)],
    Discriminator(_workstream_kind),
]

# New decisions require measurement intent; saved workstreams keep their
# original contract so a resume never invents intent for an older profile.
type PlannerWorkstream = Annotated[
    Annotated[WorkstreamPlan, Tag(WorkstreamKind.IMPLEMENT.value)]
    | Annotated[ProfileDecision, Tag(WorkstreamKind.PROFILE.value)]
    | Annotated[MergePlan, Tag(WorkstreamKind.MERGE.value)]
    | Annotated[CombinePlan, Tag(WorkstreamKind.COMBINE.value)],
    Discriminator(_workstream_kind),
]


class _AnyOfTaggedUnions(GenerateJsonSchema):
    """Render a tagged union as ``anyOf`` without the ``discriminator`` keyword.

    The strict structured-output subset agent providers accept rejects
    ``oneOf``. Each member fixes ``kind`` to its own constant, so at most one
    member matches any instance and ``anyOf`` accepts exactly what ``oneOf``
    would.
    """

    @override
    def tagged_union_schema(self, schema: core_schema.TaggedUnionSchema) -> JsonSchemaValue:
        rendered = super().tagged_union_schema(schema)
        members = rendered.get("oneOf")
        if members is None:
            return rendered
        return {"anyOf": members}


def planned_id(plan: PlannedWorkstream) -> str:
    """Return the ID a planned workstream is scheduled and tracked under."""
    if isinstance(plan, ProfilePlan):
        return plan.profile_id
    if isinstance(plan, MergePlan):
        return plan.merge_id
    return plan.hypothesis_id  # an implement or a combine: both are hypotheses


class StrategyReason(StrEnum):
    """Why the planner retires a completed direction."""

    INFEASIBLE = "infeasible"
    FALSIFIED = "falsified"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    LOWER_PRIORITY = "lower_priority"


class PlannerHypothesisUpdate(HypothesisStrategyUpdate):
    """A strategic decision with a required machine-readable reason."""

    reason_kind: StrategyReason = Field(
        description="Machine-readable category of why this direction is retired."
    )


class WorkstreamExpectation(BaseModel):
    """What the planner expects of one workstream it scheduled, and what it would do next."""

    model_config = ConfigDict(extra="forbid")

    work_id: AgentId = Field(
        description="The hypothesis or profile ID of a workstream of this reply."
    )
    expected: str = Field(min_length=1, description="What you expect this workstream to show.")
    if_it_succeeds: str = Field(
        min_length=1, description="What you would plan next if it shows what you expect."
    )
    if_it_fails: str = Field(
        min_length=1, description="What you would plan next if it fails or shows the opposite."
    )


class PlannerHandoff(BaseModel):
    """The planner's note to the planner of a later turn, which starts with no memory of this one.

    Core writes it, with the plan's reasoning and workstreams, to a handoff file in the run's
    state; later planning prompts list that file's path.
    """

    model_config = ConfigDict(extra="forbid")

    expectations: tuple[WorkstreamExpectation, ...] = Field(
        max_length=32,
        description="One entry per workstream of this reply: what you expect and what follows.",
    )
    doubts: tuple[str, ...] = Field(
        max_length=16,
        description="What you are unsure of and would check next; empty when there is nothing.",
    )


class PortfolioPlan(BaseModel):
    """A bounded batch of distinct hypothesis workstreams."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(min_length=1, description="Why this portfolio of workstreams.")
    workstreams: Annotated[
        tuple[PlannerWorkstream, ...],
        DecidedFrom("capacity", "remaining", "buildable", "mergeable", "history", "baseline"),
    ] = Field(
        min_length=1,
        max_length=32,
        description=(
            "The new workstreams to start: one entry per hypothesis (kind implement), "
            "per profile (kind profile), per merge (kind merge) or per combination of "
            "candidates (kind combine)."
        ),
    )
    hypothesis_updates: Annotated[tuple[PlannerHypothesisUpdate, ...], DecidedFrom("history")] = (
        Field(
            default=(),
            max_length=32,
            description="Parks and abandonments of completed hypotheses; empty when there are none.",
        )
    )
    handoff: PlannerHandoff = Field(
        description=(
            "Your note to the next planner, who starts without your memory: what you expect "
            "of each workstream, what you would do next on each outcome, and your open doubts."
        )
    )
    evaluation_order: Annotated[tuple[str, ...], DecidedFrom("evaluations")] = Field(
        default=(),
        max_length=64,
        description=(
            "The hypothesis or profile IDs whose waiting candidates a free trusted evaluation "
            "slot measures first, first to measure first; empty to leave the order as it is. "
            "Only read when the run lets the orchestrator rank its evaluations; ignored otherwise."
        ),
    )

    @field_validator("reasoning")
    @classmethod
    def _nonblank_reasoning(cls, value: str) -> str:
        """Require a portfolio rationale without rewriting the planner's text."""
        if not value.strip():
            message = "portfolio reasoning must not be blank"
            raise ValueError(message)
        return value

    @classmethod
    @override
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = _AnyOfTaggedUnions,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        """Return the reply schema, with the workstream union as ``anyOf`` for providers."""
        return super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )


class ImplementPortfolioPlan(PortfolioPlan):
    """The planner's reply in a run that cannot profile: its schema offers no profile kind.

    Validation is the portfolio's own, so a profile entry still parses and is
    then corrected with the reason named, while a provider that enforces the
    schema never produces one.
    """

    @classmethod
    @override
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = _AnyOfTaggedUnions,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        """Return the portfolio schema with implement, merge and combine workstreams as the entry kinds."""
        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
        # The agent reads the portfolio's description, not this class's.
        schema["description"] = PortfolioPlan.model_json_schema()["description"]
        definitions = schema["$defs"]
        del definitions["PlannerWorkstream"], definitions["ProfileDecision"]
        workstreams = schema["properties"]["workstreams"]
        workstreams["items"] = {
            "anyOf": [
                {"$ref": ref_template.format(model="WorkstreamPlan")},
                {"$ref": ref_template.format(model="MergePlan")},
                {"$ref": ref_template.format(model="CombinePlan")},
            ]
        }
        workstreams["description"] = (
            "The new workstreams to start: one entry per hypothesis (kind implement), per "
            "merge (kind merge) or per combination of candidates (kind combine)."
        )
        return schema


@dataclass(frozen=True)
class ProfileOffer:
    """What a run lets its planner ask of a profile workstream.

    ``fields`` are the measurement fields the run's capture supplies, from
    ``supported_profile_fields``: the one source for the planner's schema and prompt and for
    plan validation. A run that cannot profile has no offer at all.
    """

    fields: tuple[ProfileField, ...]


def _offer_profile_fields(schema: dict[str, Any], fields: tuple[ProfileField, ...]) -> None:
    """Restrict ``required_fields`` in a portfolio schema to the fields the capture supplies.

    With no supported field the property admits only the empty list, so the schema never
    offers a field the run cannot measure.
    """
    definitions = schema["$defs"]
    if fields:
        definitions["ProfileField"]["enum"] = [str(field) for field in fields]
        return
    del definitions["ProfileField"]
    offered = definitions["ProfileDecision"]["properties"]["required_fields"]
    del offered["items"]
    offered["maxItems"] = 0
    offered["description"] = "This run's capture supplies no named measurement field: leave empty."


def planner_reply_type(offer: ProfileOffer | None) -> type[PortfolioPlan]:
    """The planner's reply type for a run: it offers exactly what the run can do.

    Without an ``offer`` the schema has no profile workstream kind. With one, the schema
    lists the capture's fields and no other, and none when the capture supplies none.
    """
    if offer is None:
        return ImplementPortfolioPlan

    @override
    def schema(
        _cls: type[PortfolioPlan],
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = _AnyOfTaggedUnions,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        result = PortfolioPlan.model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
        _offer_profile_fields(result, offer.fields)
        return result

    return cast(
        "type[PortfolioPlan]",
        type(
            "ProfilingPortfolioPlan", (PortfolioPlan,), {"model_json_schema": classmethod(schema)}
        ),
    )


def planner_response_type(
    revisions: tuple[str, ...], offer: ProfileOffer | None
) -> type[PortfolioPlan]:
    """Bind the reply schema to the same immutable revisions pure validation offers.

    Parsing keeps the ordinary portfolio contract so an invalid selector reaches
    the portfolio correction with its field path and usable alternatives.
    """
    base = planner_reply_type(offer)

    @override
    def schema(
        _cls: type[PortfolioPlan],
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = _AnyOfTaggedUnions,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> dict[str, Any]:
        result = base.model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
        offered = sorted(set(revisions))
        definitions = result["$defs"]
        definitions["WorkstreamPlan"]["properties"]["parent_revision"]["enum"] = [None, *offered]
        if offered:  # with nothing offered no combine is valid, and validation says so
            definitions["CombineParent"]["properties"]["revision"]["enum"] = offered
        return result

    return cast(
        "type[PortfolioPlan]",
        type("OfferedPortfolioPlan", (base,), {"model_json_schema": classmethod(schema)}),
    )


class WorkResult(Value):
    """An agent's compact, evidence-linked account of the work it did in its workspace."""

    summary: str = Field(min_length=1)
    outcome: HypothesisOutcome
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)
    next_step: str = ""


class UnappliedPart(Value):
    """A part of a combine the implementer could not apply as it stands, and why."""

    hypothesis_id: str = Field(min_length=1, description="The part's hypothesis ID.")
    part: str = Field(min_length=1, description="What was left out: a hunk, a file, a behavior.")
    why: str = Field(min_length=1, description="Why it could not be applied.")


class ImplementerResult(WorkResult, RevisionReply):
    """What an implementer answers: its work, which core checkpoints unless it is blocked."""

    outcome: Annotated[
        HypothesisOutcome,
        DecidedFrom("pass_criteria", "evidence", "rejection", "prior_failure_tail"),
    ]
    unapplied: tuple[UnappliedPart, ...] = Field(
        default=(),
        description=(
            "For a combine: every part, or piece of one, you could not apply, and why. "
            "Empty when everything applied, and outside a combine."
        ),
    )

    @override
    def produces_revision(self) -> bool:
        """A blocked implementer claims no work, so core records no checkpoint for its turn."""
        return self.outcome is not HypothesisOutcome.BLOCKED


class MergeOutcomeKind(StrEnum):
    """How a merger ends its merge."""

    MERGED = "merged"
    REFUSED = "refused"


class MergeResult(RevisionReply):
    """What a merger answers: the merge it committed, or why it stopped without one."""

    outcome: Annotated[MergeOutcomeKind, DecidedFrom("candidates", "instructions", "watch_for")] = (
        Field(
            description=(
                "merged when every candidate is merged as instructed and the project's fast tests "
                "pass; refused when the instructions cannot be followed without redesigning."
            )
        )
    )
    summary: str = Field(min_length=1, description="What you merged, in order, and how.")
    tests: str = Field(
        min_length=1,
        description="The project's fast tests you ran on the merged tree, and their result.",
    )
    reason: str = Field(
        default="",
        description="For refused: which instruction could not be followed, and why. Else empty.",
    )

    @override
    def produces_revision(self) -> bool:
        """Only a merge that merged leaves a commit for core to checkpoint."""
        return self.outcome is MergeOutcomeKind.MERGED


class ProfilerResult(WorkResult):
    """What a profiler answers: it measures a revision and never produces one."""

    outcome: Annotated[HypothesisOutcome, DecidedFrom("question", "required_fields")]


class PlanOutline(BaseModel):
    """The steps of the task's plan, as the planner outlines them from the plan's documents.

    The steps are checked by the rules a ``[plan]`` table's own steps are (`outline_problems`):
    a reply that breaks one is corrected with the rule named.
    """

    model_config = ConfigDict(extra="forbid")

    steps: Annotated[tuple[PlanStep, ...], DecidedFrom("documents")] = Field(
        min_length=1,
        max_length=MAX_STEPS,
        description=(
            "The plan's steps in the order they should be implemented; each names the steps it "
            "builds on."
        ),
    )

    @field_validator("steps")
    @classmethod
    def _steps_form_a_plan(cls, steps: tuple[PlanStep, ...]) -> tuple[PlanStep, ...]:
        problems = outline_problems(steps)
        if problems:
            raise ValueError("; ".join(problems))
        return steps


class ReviewResult(BaseModel):
    """Independent decision over one candidate and its linked evidence."""

    model_config = ConfigDict(extra="forbid")

    passed: Annotated[bool, DecidedFrom("pass_criteria", "summary", "evidence", "evaluations")]
    analysis: str = Field(min_length=1)
    feedback: str = ""


__all__ = [
    "MAX_COMBINE_PARENTS",
    "MAX_PROFILE_QUESTION_CHARS",
    "CombineParent",
    "CombinePlan",
    "EvidenceAttributionError",
    "EvidenceReference",
    "ImplementPortfolioPlan",
    "ImplementerResult",
    "MergeOutcomeKind",
    "MergePlan",
    "MergeResult",
    "PlanOutline",
    "PlannedWorkstream",
    "PlannerHandoff",
    "PlannerHypothesisUpdate",
    "PlannerWorkstream",
    "PortfolioPlan",
    "ProfileDecision",
    "ProfileField",
    "ProfileOffer",
    "ProfilePlan",
    "ProfilerResult",
    "ReviewResult",
    "StrategyReason",
    "UnappliedPart",
    "WorkResult",
    "WorkstreamExpectation",
    "WorkstreamKind",
    "WorkstreamPlan",
    "planned_id",
    "planner_reply_type",
    "planner_response_type",
]
