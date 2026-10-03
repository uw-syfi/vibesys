"""Strict contracts and durable state for dynamic hypothesis portfolios."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Self, override

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.hypothesis.plan import HypothesisStrategyUpdate
from vibesys.orchestration.hypothesis.state import HypothesisState
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import AgentId, MetricDirection

if TYPE_CHECKING:
    from pydantic.config import ExtraValues


class DynamicOptions(AgentOrchestrationOptions):
    """Validated policy controls for the dynamic orchestration."""

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


class EvidenceReference(BaseModel):
    """Compact pointer to evidence stored outside agent conversation history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str = Field(min_length=1, max_length=512)
    purpose: str = Field(min_length=1, max_length=256)
    revision: str | None = Field(default=None, min_length=1, max_length=256)


class WorkstreamPlan(BaseModel):
    """One causally independent hypothesis selected for parallel work."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: AgentId
    title: str = Field(min_length=1, max_length=80)
    hypothesis: str = Field(min_length=1, max_length=2000)
    task: str = Field(min_length=1, max_length=4000)
    pass_criteria: str = Field(min_length=1, max_length=2000)
    continue_hypothesis: bool = False
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)


class PortfolioPlan(BaseModel):
    """A bounded batch of distinct hypothesis workstreams."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        min_length=1, max_length=2000, description="Why this portfolio of workstreams."
    )
    workstreams: tuple[WorkstreamPlan, ...] = Field(
        min_length=1,
        max_length=32,
        description="The new workstreams to start, one entry per hypothesis.",
    )
    hypothesis_updates: tuple[HypothesisStrategyUpdate, ...] = Field(
        default=(),
        max_length=32,
        description="Parks and abandonments of completed hypotheses; empty when there are none.",
    )

    @model_validator(mode="after")
    def _distinct_hypotheses(self) -> PortfolioPlan:
        identifiers = [item.hypothesis_id for item in self.workstreams]
        repeated = sorted({item for item in identifiers if identifiers.count(item) > 1})
        if repeated:
            message = (
                f"portfolio workstreams must use distinct hypothesis IDs; repeated: {repeated}"
            )
            raise ValueError(message)
        return self


class ImplementerResult(BaseModel):
    """An implementer's compact, evidence-linked result."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    outcome: HypothesisOutcome
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=8)
    next_step: str = Field(default="", max_length=1000)


class ReviewResult(BaseModel):
    """Independent decision over one candidate and its linked evidence."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    analysis: str = Field(min_length=1, max_length=2000)
    feedback: str = Field(default="", max_length=1000)


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

    @property
    def accepted(self) -> bool:
        """Return whether every configured gate represented here passed."""
        local = self.local_validation_passed is not False
        accuracy = self.accuracy_passed is not False
        benchmark = self.benchmark_passed is not False
        return local and accuracy and benchmark


class WorkstreamPhase(StrEnum):
    """Recoverable progress states for one isolated candidate."""

    PENDING = "pending"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    REVIEWED = "reviewed"
    EVALUATED = "evaluated"
    FAILED = "failed"


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


class DynamicState(BaseModel):
    """The dynamic plugin's complete durable aggregate."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[6] = 6
    experiment_revision: Annotated[int, Field(ge=0)] = 0
    next_planning_call: Annotated[int, Field(gt=0)] = 1
    search: HypothesisState = Field(default_factory=HypothesisState)
    workstreams: list[DynamicWorkstream] = Field(default_factory=list)
    # The input (root) revision's trusted benchmark, measured once per run.
    # Candidates must beat it to be adopted or built on.
    baseline: EvaluationResult | None = None
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
        identifiers = [item.hypothesis_id for item in self.workstreams]
        if len(identifiers) != len(set(identifiers)):
            message = "dynamic state contains duplicate hypothesis IDs"
            raise ValueError(message)
        return self


# Keys that schema version 1 wrote and version 2 retired: evaluation-cadence
# bookkeeping (every review-passed candidate is evaluated), a member ID that
# always equaled the hypothesis ID, and a per-plan evaluation request with no
# effect. Version 3 renamed the planning-call index from "epoch"; version 4
# moved the attempt counters into one budget; version 5 added
# ``implementer_started``, derived from the budget for older states. Version 6
# dropped the implementer result's ``validation_recipe_artifact``, which no
# prompt documented.
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
    """Upgrade an older state mapping to version 6.

    Version 1 loses its retired keys; versions 1 and 2 rename the planning-call
    index from ``epoch``; versions 1 to 3 move ``attempts`` and
    ``refunded_attempts`` into ``budget``; versions 1 to 4 derive
    ``implementer_started`` from it; versions 1 to 5 drop
    ``validation_recipe_artifact`` from each implementation. Only a mapping that declares an
    older version (or no version, which loaded as 1) is rewritten, so a current
    state with an unknown key is still rejected.
    """
    if not isinstance(data, dict):
        return data
    version = data.get("schema_version", 1)
    if version not in {1, 2, 3, 4, 5}:
        return data
    migrated = {key: value for key, value in data.items() if key not in _RETIRED_STATE_KEYS}
    migrated = _renamed(migrated, "next_epoch", "next_planning_call")
    migrated["schema_version"] = 6
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
    "DynamicOptions",
    "DynamicState",
    "DynamicWorkstream",
    "EvaluationResult",
    "EvidenceReference",
    "ImplementerResult",
    "PortfolioPlan",
    "ReviewResult",
    "WorkstreamBudget",
    "WorkstreamPhase",
    "WorkstreamPlan",
]
