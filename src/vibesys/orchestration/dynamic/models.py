"""Strict contracts and durable state for dynamic hypothesis portfolios."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast, override

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)
from pydantic.json_schema import GenerateJsonSchema

from vibesys.hypothesis import HypothesisOutcome
from vibesys.hypothesis.plan import HypothesisStrategyUpdate
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vs_runtime.api import (
    AgentId,
    ProfileField,
)

if TYPE_CHECKING:
    from pydantic.json_schema import JsonSchemaMode, JsonSchemaValue
    from pydantic_core import core_schema


class DynamicOptions(AgentOrchestrationOptions):
    """Validated policy controls for the dynamic orchestration.

    The run schedules at most ``max_rounds * max_in_flight`` workstreams, new
    or continued alike: each is one round of agent work.
    """

    max_in_flight: Annotated[int, Field(gt=0, le=32)] = 2
    # An attempt ends once this many of its evaluations in a row fail with
    # one failure signature (exception type and innermost source line).
    max_repeated_failures: Annotated[int, Field(ge=2, le=32)] = 3
    # Turns of one workstream that may end without a candidate reaching measurement before
    # it settles as failed and its slot returns to the planner.
    max_unmeasured_turns: Annotated[int, Field(gt=0, le=32)] = 2
    # Seconds to wait before asking an agent again after the provider connection dropped
    # its turn; doubles with each further drop of the same turn.
    turn_drop_backoff_seconds: Annotated[float, Field(gt=0, le=600)] = 5.0

    @model_validator(mode="after")
    def _supported_interface(self) -> DynamicOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported dynamic interface {self.interface!r}"
            raise ValueError(message)
        return self


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
    continue_hypothesis: bool = False
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)
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
    | Annotated[ProfilePlan, Tag(WorkstreamKind.PROFILE.value)],
    Discriminator(_workstream_kind),
]

# New decisions require measurement intent; saved workstreams keep their
# original contract so a resume never invents intent for an older profile.
type PlannerWorkstream = Annotated[
    Annotated[WorkstreamPlan, Tag(WorkstreamKind.IMPLEMENT.value)]
    | Annotated[ProfileDecision, Tag(WorkstreamKind.PROFILE.value)],
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
    return plan.hypothesis_id


class StrategyReason(StrEnum):
    """Why the planner retires a completed direction."""

    INFEASIBLE = "infeasible"
    FALSIFIED = "falsified"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    LOWER_PRIORITY = "lower_priority"


class PlannerHypothesisUpdate(HypothesisStrategyUpdate):
    """A strategic decision with a required machine-readable reason."""

    reason_kind: StrategyReason


class PortfolioPlan(BaseModel):
    """A bounded batch of distinct hypothesis workstreams."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(min_length=1, description="Why this portfolio of workstreams.")
    workstreams: tuple[PlannerWorkstream, ...] = Field(
        min_length=1,
        max_length=32,
        description=(
            "The new workstreams to start: one entry per hypothesis (kind implement) "
            "or per profile (kind profile)."
        ),
    )
    hypothesis_updates: tuple[PlannerHypothesisUpdate, ...] = Field(
        default=(),
        max_length=32,
        description="Parks and abandonments of completed hypotheses; empty when there are none.",
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
        """Return the portfolio schema with implement workstreams as the only entry kind."""
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
        workstreams["items"] = {"$ref": ref_template.format(model="WorkstreamPlan")}
        workstreams["description"] = "The new workstreams to start: one entry per hypothesis."
        return schema


def planner_response_type(revisions: tuple[str, ...], *, profiling: bool) -> type[PortfolioPlan]:
    """Bind the reply schema to the same immutable revisions pure validation offers.

    Parsing keeps the ordinary portfolio contract so an invalid selector reaches
    the portfolio correction with its field path and usable alternatives.
    """
    base = PortfolioPlan if profiling else ImplementPortfolioPlan

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
        result["$defs"]["WorkstreamPlan"]["properties"]["parent_revision"]["enum"] = [
            None,
            *sorted(set(revisions)),
        ]
        return result

    return cast(
        "type[PortfolioPlan]",
        type("OfferedPortfolioPlan", (base,), {"model_json_schema": classmethod(schema)}),
    )


class ImplementerResult(BaseModel):
    """An implementer's compact, evidence-linked result."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    outcome: HypothesisOutcome
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)
    next_step: str = ""


class ReviewResult(BaseModel):
    """Independent decision over one candidate and its linked evidence."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    analysis: str = Field(min_length=1)
    feedback: str = ""


class WaitingForDependency(BaseModel):
    """End this agent turn until every owned evaluation handle settles."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    handles: tuple[str, ...] = Field(min_length=1)

    @field_validator("handles")
    @classmethod
    def _unique_nonempty_handles(cls, handles: tuple[str, ...]) -> tuple[str, ...]:
        if any(not handle.strip() or handle != handle.strip() for handle in handles) or len(
            handles
        ) != len(set(handles)):
            message = "handles must be nonblank and unique"
            raise ValueError(message)
        return handles


class WaitingForEvaluation(WaitingForDependency):
    """End this agent turn until every owned evaluation handle settles."""

    kind: Literal["waiting_for_evaluation"]


def _reply_kind(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("kind", "result"))
    return str(getattr(value, "kind", "result"))


type ImplementerReply = Annotated[
    Annotated[ImplementerResult, Tag("result")]
    | Annotated[WaitingForEvaluation, Tag("waiting_for_evaluation")],
    Discriminator(_reply_kind),
]
type JudgeReply = Annotated[
    Annotated[ReviewResult, Tag("result")]
    | Annotated[WaitingForEvaluation, Tag("waiting_for_evaluation")],
    Discriminator(_reply_kind),
]


__all__ = [
    "MAX_PROFILE_QUESTION_CHARS",
    "DynamicOptions",
    "EvidenceAttributionError",
    "EvidenceReference",
    "ImplementPortfolioPlan",
    "ImplementerReply",
    "ImplementerResult",
    "JudgeReply",
    "PlannedWorkstream",
    "PlannerHypothesisUpdate",
    "PlannerWorkstream",
    "PortfolioPlan",
    "ProfileDecision",
    "ProfilePlan",
    "ReviewResult",
    "StrategyReason",
    "WaitingForEvaluation",
    "WorkstreamKind",
    "WorkstreamPlan",
    "planned_id",
    "planner_response_type",
]
