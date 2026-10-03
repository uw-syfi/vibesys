"""Strict contracts and durable state for dynamic hypothesis portfolios."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self, override

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    FiniteFloat,
    Tag,
    model_validator,
)
from pydantic.json_schema import GenerateJsonSchema

from vibesys.hypothesis.plan import HypothesisStrategyUpdate
from vibesys.hypothesis.state import HypothesisState
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import (
    AgentId,
    CandidateProfile,
    CandidateProfileStatus,
    MetricDirection,
    PartialMeasurement,
)

if TYPE_CHECKING:
    from pydantic.config import ExtraValues
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
class EvidenceReference(BaseModel):
    """Compact pointer to evidence stored outside agent conversation history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str = Field(min_length=1, max_length=512)
    purpose: str = Field(min_length=1, max_length=1000)
    revision: str | None = Field(default=None, min_length=1, max_length=256)


class WorkstreamKind(StrEnum):
    """What a scheduled workstream does with its slot."""

    # Implement a hypothesis in a candidate workspace (the default).
    IMPLEMENT = "implement"
    # Profile an existing candidate revision to inform later plans.
    PROFILE = "profile"


class WorkstreamPlan(BaseModel):
    """One causally independent hypothesis selected for parallel work."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[WorkstreamKind.IMPLEMENT] = WorkstreamKind.IMPLEMENT
    hypothesis_id: AgentId
    title: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    task: str = Field(min_length=1)
    pass_criteria: str = Field(min_length=1)
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


# A profile question is capped wide enough that a paragraph never reaches it,
# and below the profiler service's request limit.
MAX_PROFILE_QUESTION_CHARS = 4000


class ProfilePlan(BaseModel):
    """One profile of an existing candidate revision, scheduled in a slot."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[WorkstreamKind.PROFILE]
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
        description="What the profile must answer to inform the next plan.",
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


class PortfolioPlan(BaseModel):
    """A bounded batch of distinct hypothesis workstreams."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(min_length=1, description="Why this portfolio of workstreams.")
    workstreams: tuple[PlannedWorkstream, ...] = Field(
        min_length=1,
        max_length=32,
        description=(
            "The new workstreams to start: one entry per hypothesis (kind implement) "
            "or per profile (kind profile)."
        ),
    )
    hypothesis_updates: tuple[HypothesisStrategyUpdate, ...] = Field(
        default=(),
        max_length=32,
        description="Parks and abandonments of completed hypotheses; empty when there are none.",
    )

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
        del definitions["PlannedWorkstream"], definitions["ProfilePlan"]
        workstreams = schema["properties"]["workstreams"]
        workstreams["items"] = {"$ref": ref_template.format(model="WorkstreamPlan")}
        workstreams["description"] = "The new workstreams to start: one entry per hypothesis."
        return schema


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


class EvaluationResult(BaseModel):
    """Trusted evaluation facts recorded against an exact candidate revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str
    local_validation_passed: bool | None = None
    local_validation_feedback: str | None = None
    accuracy_passed: bool | None = None
    accuracy_feedback: str | None = None
    benchmark_passed: bool | None = None
    benchmark_feedback: str | None = None
    metric_name: str | None = None
    metric_value: FiniteFloat | None = None
    metric_direction: MetricDirection | None = None
    metric_unit: str | None = None
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    # What the failed benchmark measured before it stopped, as its evaluator
    # reported it; absent when it reported nothing.
    partial_measurement: PartialMeasurement | None = None

    @property
    def accepted(self) -> bool:
        """Return whether every configured gate represented here passed."""
        local = self.local_validation_passed is not False
        accuracy = self.accuracy_passed is not False
        benchmark = self.benchmark_passed is not False
        return local and accuracy and benchmark


class VerifiedCandidate(BaseModel):
    """A revision whose exact content passed trusted accuracy in an agent-submitted evaluation.

    The revision is retained when it is recorded, and ``content_digest`` is the
    digest the evaluation service computed for it, so a later reader can check
    that the revision still reproduces the evaluated content.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The same evaluation's benchmark verdict (None when it ran no benchmark)
    # and its headline measurement, when it recorded one.
    benchmark_passed: bool | None = None
    metric_name: str | None = None
    metric_value: FiniteFloat | None = None
    metric_unit: str | None = None
    metric_direction: MetricDirection | None = None
    # What that benchmark measured before it failed, as its evaluator reported it.
    partial_measurement: PartialMeasurement | None = None


class WorkstreamPhase(StrEnum):
    """Recoverable progress states for one isolated candidate."""

    PENDING = "pending"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    REVIEWED = "reviewed"
    EVALUATED = "evaluated"
    FAILED = "failed"
    PARKED = "parked"  # Resumable: worktree retained, session kept, meter stopped.
    CANCELLED = "cancelled"  # Terminal: round recorded, jobs released.


class WorkstreamBudget(BaseModel):
    """Durable retry budget of one workstream; every retry decision derives from it.

    An attempt is charged when an implementer turn starts, so a turn cut off by
    a crash stays charged, and when a failed attempt ran no implementer turn (a
    review, evaluation, or workspace failure), so a failure that repeats ends.
    An interrupted implementer turn is refunded at most ``limit`` times: resume
    redoes it, but a turn that crashes the process every time still ends. A
    continued hypothesis is a new workstream and starts with a fresh budget.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spent: Annotated[int, Field(ge=0)] = 0
    refunded: Annotated[int, Field(ge=0)] = 0

    def remaining(self, limit: int) -> int:
        """Return how many more attempts ``limit`` allows."""
        return max(limit - self.spent, 0)

    def charge(self) -> WorkstreamBudget:
        """Spend one attempt."""
        return self.model_copy(update={"spent": self.spent + 1})

    def exhaust(self, limit: int) -> WorkstreamBudget:
        """Spend every remaining attempt, so neither the run nor resume retries."""
        return self.model_copy(update={"spent": max(self.spent, limit)})

    def refund_interrupted(self, limit: int) -> WorkstreamBudget | None:
        """Uncount an interrupted implementer turn, or ``None`` once ``limit`` refunds are used."""
        if self.refunded >= limit:
            return None
        return self.model_copy(
            update={"spent": max(self.spent - 1, 0), "refunded": self.refunded + 1}
        )


class DynamicWorkstream(BaseModel):
    """Durable lifecycle for one stable hypothesis identity."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: AgentId
    # Unique, increasing workstream number; also its recorded round number.
    sequence: Annotated[int, Field(gt=0)]
    # Index of the planning call that scheduled this workstream. Under slot
    # refill a call plans only the free slots, so it is not a batch boundary.
    planning_call: Annotated[int, Field(gt=0)]
    plan: WorkstreamPlan
    parent_revision: str
    phase: WorkstreamPhase = WorkstreamPhase.PENDING
    budget: WorkstreamBudget = Field(default_factory=WorkstreamBudget)
    candidate_revision: str | None = None
    implementation: ImplementerResult | None = None
    review: ReviewResult | None = None
    evaluation: EvaluationResult | None = None
    # Compact record of this hypothesis's previous attempt, shown to a
    # continued implementer. Its session normally resumes (the candidate path
    # is keyed by hypothesis); the record covers a session that did not.
    prior_attempt: str = ""
    # The candidate revision the previous attempt ended at, so a continued
    # implementer is told what its reset worktree changed.
    prior_revision: str | None = None
    # Correction guidance (review or trusted-evaluation failure) for the next
    # implementation attempt; persisted so a retry after a crash receives it.
    feedback: str | None = None
    # Error of the latest failed attempt, shown to the planner.
    last_error: str | None = None
    # Whether that attempt failed before any agent turn started (in setup).
    setup_failure: bool = False
    # Whether an implementer turn of this workstream has started. A setup
    # failure charges the budget without a turn, so the budget cannot say.
    implementer_started: bool = False
    # The latest revision of this hypothesis whose content passed accuracy in
    # an agent-submitted evaluation; a new workstream may build on it.
    verified: VerifiedCandidate | None = None

    @model_validator(mode="after")
    def _stable_identity(self) -> DynamicWorkstream:
        if self.plan.hypothesis_id != self.hypothesis_id:
            message = "workstream plan must preserve its hypothesis_id"
            raise ValueError(message)
        implemented = self.phase in {
            WorkstreamPhase.IMPLEMENTED,
            WorkstreamPhase.REVIEWED,
            WorkstreamPhase.EVALUATED,
        }
        if implemented and (self.candidate_revision is None or self.implementation is None):
            message = f"{self.phase.value} workstream requires a retained implementation"
            raise ValueError(message)
        if (
            self.phase in {WorkstreamPhase.REVIEWED, WorkstreamPhase.EVALUATED}
            and self.review is None
        ):
            message = f"{self.phase.value} workstream requires a review"
            raise ValueError(message)
        if self.phase is WorkstreamPhase.EVALUATED and self.evaluation is None:
            message = "evaluated workstream requires trusted evaluation evidence"
            raise ValueError(message)
        return self


class DynamicProfile(BaseModel):
    """Durable record of one profile workstream and its trusted outcome.

    It shares the workstream sequence, so it spends one unit of the workstream
    budget unless it ends unsupported, but records no round: a profile
    produces no candidate.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: AgentId
    sequence: Annotated[int, Field(gt=0)]
    planning_call: Annotated[int, Field(gt=0)]
    plan: ProfilePlan
    # The exact revision profiled, resolved from the target when scheduled.
    revision: str = Field(min_length=1)
    # None until the profile ends; resume runs a profile without an outcome.
    outcome: CandidateProfile | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DynamicProfile:
        if self.plan.profile_id != self.profile_id:
            message = "profile plan must preserve its profile_id"
            raise ValueError(message)
        if self.outcome is not None and self.outcome.revision != self.revision:
            message = "profile outcome must describe the scheduled revision"
            raise ValueError(message)
        return self


class InputMeasurementAttempts(BaseModel):
    """Durable submission budget for one immutable input revision."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    revision: str = Field(min_length=1)
    attempts: Annotated[int, Field(ge=0)] = 0


class InputNotMeasurable(BaseModel):
    """A trusted workload rejection of the input, supplied to the planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1)


class Expectation(BaseModel):
    """Expected progress of a workstream, retained for comparison with observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    milestone: str
    expected_minutes: Annotated[float, Field(ge=2, le=240)]
    reason: str


class QueuedStart(BaseModel):
    """A durable start request awaiting a slot, consumed by the step-2 host core."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: PlannedWorkstream
    expectation: Expectation
    priority: Literal["now", "next", "later"] = "now"


class SteerNote(BaseModel):
    """A durable note, marked delivered or dropped by worker control."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    note_sha256: str
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    sent_at_s: float
    interrupt: bool
    delivered_to: str | None = None
    dropped: Literal["workstream_settled"] | None = None


class JournalEntry(BaseModel):
    """One durable orchestrator decision, independent of provider history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at_s: float
    turn: int
    kind: Literal["start", "steer", "park", "cancel", "reflect", "turn_report", "finish"]
    subject: AgentId | None
    text: str


class AgentLoopState(BaseModel):
    """Persisted agent-loop decisions; planner runs leave this sub-state absent."""

    model_config = ConfigDict(extra="forbid")

    generation: Annotated[int, Field(ge=1)] = 1
    turns: Annotated[int, Field(ge=0)] = 0
    expectations: dict[AgentId, Expectation] = Field(default_factory=dict)
    queue: list[QueuedStart] = Field(default_factory=list)
    steers: dict[AgentId, list[SteerNote]] = Field(default_factory=dict)
    # The host retains the last 200 entries; older entries move to an artifact.
    journal: list[JournalEntry] = Field(default_factory=list)
    next_check_in_s: float | None = None
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    capabilities_withdrawn: list[Literal["profile"]] = Field(default_factory=list)
    finished: str | None = None


class DynamicState(BaseModel):
    """The dynamic plugin's complete durable aggregate."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[7] = 7
    agent: AgentLoopState | None = None
    experiment_revision: Annotated[int, Field(ge=0)] = 0
    next_planning_call: Annotated[int, Field(gt=0)] = 1
    search: HypothesisState = Field(default_factory=HypothesisState)
    workstreams: list[DynamicWorkstream] = Field(default_factory=list)
    profiles: list[DynamicProfile] = Field(default_factory=list)
    # The input (root) revision's trusted benchmark, measured once per run.
    # Candidates must beat it to be adopted or built on.
    baseline: EvaluationResult | None = None
    input_measurement: InputMeasurementAttempts | None = None
    winner_revision: str | None = None
    adoption_pending: bool = False

    @classmethod
    @override
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Load persisted JSON, first upgrading state written by an older version.

        The migration rewrites the parsed mapping and validates the re-encoded
        JSON, so the load keeps JSON-mode validation. A ``mode="before"`` model
        validator would instead hand the mapping to a Python-mode validation,
        which under the state store's ``strict=True`` rejects JSON arrays for
        tuple fields.
        """
        try:
            parsed = json.loads(json_data)
        except ValueError:
            # Malformed JSON: pydantic reports it in its own words.
            return super().model_validate_json(
                json_data,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        return super().model_validate_json(
            json.dumps(_migrate_state(parsed)),
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    @model_validator(mode="after")
    def _valid_history(self) -> DynamicState:
        identifiers = [
            *(item.hypothesis_id for item in self.workstreams),
            *(item.profile_id for item in self.profiles),
        ]
        if len(identifiers) != len(set(identifiers)):
            message = "dynamic state contains duplicate hypothesis or profile IDs"
            raise ValueError(message)
        sequences = [
            *(item.sequence for item in self.workstreams),
            *(item.sequence for item in self.profiles),
        ]
        if len(sequences) != len(set(sequences)):
            message = "dynamic state contains duplicate workstream sequences"
            raise ValueError(message)
        return self

    def unsupported_profiles(self) -> int:
        """Return how many profiles ended unsupported: no capture ran for them.

        The first one proves the run cannot profile, so policy stops offering
        profiles; the outcomes themselves are the durable record of that.
        """
        return sum(
            item.outcome is not None and item.outcome.status is CandidateProfileStatus.UNSUPPORTED
            for item in self.profiles
        )

    def scheduled(self) -> int:
        """Return the largest sequence scheduled so far, implement or profile."""
        return max(
            (
                *(item.sequence for item in self.workstreams),
                *(item.sequence for item in self.profiles),
            ),
            default=0,
        )


# Keys that schema version 1 wrote and version 2 retired: evaluation-cadence
# bookkeeping (every review-passed candidate is evaluated), a member ID that
# always equaled the hypothesis ID, and a per-plan evaluation request with no
# effect. Version 3 renamed the planning-call index from "epoch"; version 4
# moved the attempt counters into one budget; version 5 added
# ``implementer_started``, derived from the budget for older states. Version 6
# dropped the implementer result's ``validation_recipe_artifact``, which no
# prompt documented. Version 7 adds optional agent-loop state without changing
# existing planner data.
_PLANNER_STATE_VERSION = 6
_RETIRED_STATE_KEYS = frozenset({"eligible_evaluation_candidates"})
_RETIRED_WORKSTREAM_KEYS = frozenset(
    {"member_id", "evaluation_eligibility_counted", "cadence_evaluation_due"}
)
_RETIRED_PLAN_KEYS = frozenset({"request_evaluation"})
_RETIRED_IMPLEMENTATION_KEYS = frozenset({"validation_recipe_artifact"})


def _without(data: object, keys: frozenset[str]) -> object:
    if not isinstance(data, dict):
        return data
    return {key: value for key, value in data.items() if key not in keys}


def _renamed(data: dict[str, object], old: str, new: str) -> dict[str, object]:
    return {new if key == old else key: value for key, value in data.items()}


def _migrate_state(data: object) -> object:
    """Upgrade an older state mapping to version 7.

    Version 1 loses its retired keys; versions 1 and 2 rename the planning-call
    index from ``epoch``; versions 1 to 3 move ``attempts`` and
    ``refunded_attempts`` into ``budget``; versions 1 to 4 derive
    ``implementer_started`` from it; versions 1 to 5 drop
    ``validation_recipe_artifact`` from each implementation. Version 6 changes
    only the schema version; the optional agent sub-state defaults to None.
    Only a mapping that declares an older version (or no version, which loaded
    as 1) is rewritten, so a current state with an unknown key is still rejected.
    """
    if not isinstance(data, dict):
        return data
    version = data.get("schema_version", 1)
    if version == _PLANNER_STATE_VERSION:
        return {**data, "schema_version": 7}
    if version not in {1, 2, 3, 4, 5}:
        return data
    migrated = {key: value for key, value in data.items() if key not in _RETIRED_STATE_KEYS}
    migrated = _renamed(migrated, "next_epoch", "next_planning_call")
    migrated["schema_version"] = 7
    workstreams = migrated.get("workstreams")
    if isinstance(workstreams, list):
        migrated["workstreams"] = [_migrate_workstream(item) for item in workstreams]
    return migrated


def _migrate_workstream(data: object) -> object:
    if not isinstance(data, dict):
        return data
    item = {key: value for key, value in data.items() if key not in _RETIRED_WORKSTREAM_KEYS}
    item = _renamed(item, "epoch", "planning_call")
    if "plan" in item:
        item["plan"] = _without(item["plan"], _RETIRED_PLAN_KEYS)
    if "implementation" in item:
        item["implementation"] = _without(item["implementation"], _RETIRED_IMPLEMENTATION_KEYS)
    if "budget" not in item:
        item["budget"] = {
            "spent": item.pop("attempts", 0),
            "refunded": item.pop("refunded_attempts", 0),
        }
    budget = item["budget"]
    if isinstance(budget, dict):
        # Before version 5 only a charge said a turn may have run.
        item.setdefault(
            "implementer_started", budget.get("spent", 0) > 0 or budget.get("refunded", 0) > 0
        )
    return item


__all__ = [
    "MAX_PROFILE_QUESTION_CHARS",
    "AgentLoopState",
    "DynamicOptions",
    "DynamicProfile",
    "DynamicState",
    "DynamicWorkstream",
    "EvaluationResult",
    "EvidenceReference",
    "Expectation",
    "ImplementPortfolioPlan",
    "ImplementerResult",
    "InputMeasurementAttempts",
    "InputNotMeasurable",
    "JournalEntry",
    "PlannedWorkstream",
    "PortfolioPlan",
    "ProfilePlan",
    "QueuedStart",
    "ReviewResult",
    "SteerNote",
    "VerifiedCandidate",
    "WorkstreamBudget",
    "WorkstreamKind",
    "WorkstreamPhase",
    "WorkstreamPlan",
    "planned_id",
]
