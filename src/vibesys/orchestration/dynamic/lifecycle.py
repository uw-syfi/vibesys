"""Pure durable lifecycle transitions, persisted by the dynamic shell before effects.

``step`` never mutates its input. Unfinished requests retain stable identities
across replay; handlers acknowledge only after their idempotent effect finishes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentKind(StrEnum):
    """The lifecycle transitions supported by this repair."""

    PARK = "park"
    CANCEL = "cancel"
    INTERRUPT = "interrupt"
    TURN = "turn"
    REOPEN = "reopen"
    OBSERVE = "observe_evaluations"
    RESUME = "resume_agent_turn"


class IntentStage(StrEnum):
    """Durable dispatch progress, including explicitly unresolved effects."""

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class EvaluationOutcome(StrEnum):
    """Trusted terminal outcomes; unknown observations cannot settle a dependency."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class EvaluationDependency(BaseModel):
    """Host-validated ownership and immutable measurement identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    handle: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: Annotated[int, Field(ge=0)]
    candidate_revision: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContinuationStatus(StrEnum):
    """Withdrawal fences continuation dispatch without discarding evidence."""

    ACTIVE = "active"
    PARKED = "parked"
    CANCELLED = "cancelled"


type EvaluationEvidenceId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EvaluationContinuation(BaseModel):
    """Durable wait-all authority for one yielded agent turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    continuation_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: Annotated[int, Field(ge=1)]
    role: Literal["implementer", "judge"]
    session_key: str = Field(min_length=1)
    yielded_invocation_id: str = Field(min_length=1)
    retained_revision: str = Field(min_length=1)
    original_stage: Literal["implementing", "implemented"]
    evaluation_scope_id: str = Field(min_length=1)
    evaluation_generation: Annotated[int, Field(ge=0)]
    dependencies: tuple[EvaluationDependency, ...] = Field(min_length=1)
    settlements: dict[str, EvaluationOutcome] = Field(default_factory=dict)
    evidence_ids: dict[str, tuple[EvaluationEvidenceId, ...]] = Field(default_factory=dict)
    status: ContinuationStatus = ContinuationStatus.ACTIVE
    park_operation_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _owned_dependencies(self) -> EvaluationContinuation:
        handles = [dependency.handle for dependency in self.dependencies]
        if len(handles) != len(set(handles)):
            message = "continuation.dependencies handles must be unique"
            raise ValueError(message)
        for dependency in self.dependencies:
            if (dependency.scope_id, dependency.generation) != (
                self.evaluation_scope_id,
                self.evaluation_generation,
            ):
                message = "continuation.dependencies must belong to scope_id and generation"
                raise ValueError(message)
        if any(len(ids) != len(set(ids)) for ids in self.evidence_ids.values()):
            message = "continuation.evidence_ids must be unique per handle"
            raise ValueError(message)
        if (
            self.park_operation_id is not None
            and self.park_operation_id != self.park_operation_id.strip()
        ):
            message = "continuation.park_operation_id must not contain surrounding whitespace"
            raise ValueError(message)
        if set(self.evidence_ids) - set(self.settlements):
            message = "continuation.evidence_ids must belong to settled handles"
            raise ValueError(message)
        if set(self.settlements) - set(handles):
            message = "continuation.settlements contains an unowned handle"
            raise ValueError(message)
        if EvaluationOutcome.UNKNOWN in self.settlements.values():
            message = "continuation.settlements cannot contain unknown outcomes"
            raise ValueError(message)
        expected = "implementing" if self.role == "implementer" else "implemented"
        if self.original_stage != expected:
            message = "continuation.original_stage differs from role"
            raise ValueError(message)
        return self

    @property
    def settled(self) -> bool:
        """Whether all owned handles have trusted terminal observations."""
        return len(self.settlements) == len(self.dependencies)


class ObserveEvaluations(BaseModel):
    """Reconcile durable evaluation state before registering host waits."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["observe_evaluations"] = "observe_evaluations"
    operation_id: str
    continuation: EvaluationContinuation
    stage: IntentStage = IntentStage.PREPARED

    @property
    def scope_id(self) -> str:
        """Project lifecycle ownership from the continuation."""
        return self.continuation.scope_id

    @property
    def generation(self) -> int:
        """Project the owning workstream generation."""
        return self.continuation.generation


class ResumeAgentTurn(BaseModel):
    """Resume the same session under one stable invocation identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["resume_agent_turn"] = "resume_agent_turn"
    operation_id: str
    invocation_id: str
    continuation: EvaluationContinuation
    stage: IntentStage = IntentStage.PREPARED

    @property
    def scope_id(self) -> str:
        """Project lifecycle ownership from the continuation."""
        return self.continuation.scope_id

    @property
    def generation(self) -> int:
        """Project the owning workstream generation."""
        return self.continuation.generation

    @property
    def reconcile_only(self) -> bool:
        """Dispatched acceptance must be inspected before any replay."""
        return self.stage is IntentStage.DISPATCHED


class LifecycleIntent(BaseModel):
    """One logical request; its identity must never be reused for another payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: Annotated[int, Field(ge=1)]
    kind: IntentKind
    stage: IntentStage = IntentStage.PREPARED
    resume_revision: str | None = Field(default=None, min_length=1)
    invocation_id: str | None = Field(default=None, min_length=1)
    continuation_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _turn_identity(self) -> LifecycleIntent:
        if (self.kind in {IntentKind.OBSERVE, IntentKind.RESUME}) != (
            self.continuation_id is not None
        ):
            message = "continuation_id belongs to observe and resume intents and is required"
            raise ValueError(message)
        if self.kind is IntentKind.TURN and self.invocation_id != self.operation_id:
            message = "turn invocation_id must equal operation_id"
            raise ValueError(message)
        if self.kind is not IntentKind.TURN and self.invocation_id is not None:
            message = "invocation_id belongs only to turn intents"
            raise ValueError(message)
        return self


type LifecycleRequest = LifecycleIntent | ObserveEvaluations | ResumeAgentTurn


class LifecycleState(BaseModel):
    """The intent ledger embedded in the atomic run envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    intents: dict[str, LifecycleIntent] = Field(default_factory=dict)
    continuations: dict[str, EvaluationContinuation] = Field(default_factory=dict)
    stopped: bool = False

    @model_validator(mode="after")
    def _operation_keys(self) -> LifecycleState:
        for key, intent in self.intents.items():
            if key != intent.operation_id:
                message = f"lifecycle.intents key {key!r} differs from operation_id {intent.operation_id!r}"
                raise ValueError(message)
        for intent in self.intents.values():
            if intent.continuation_id is None:
                continue
            continuation = self.continuations.get(intent.continuation_id)
            if continuation is None or (intent.scope_id, intent.generation) != (
                continuation.scope_id,
                continuation.generation,
            ):
                message = "lifecycle intent.continuation_id must reference its owned continuation"
                raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _continuation_authority(self) -> LifecycleState:
        for key, continuation in self.continuations.items():
            _validate_park_authority(self, continuation)
            yielded = self.intents.get(continuation.yielded_invocation_id)
            observe = self.intents.get(f"{key}/observe")
            if (
                yielded is None
                or yielded.kind not in {IntentKind.TURN, IntentKind.RESUME}
                or yielded.stage is not IntentStage.COMPLETED
                or (yielded.scope_id, yielded.generation)
                != (continuation.scope_id, continuation.generation)
            ):
                message = "lifecycle continuation requires its completed owned yielded intent"
                raise ValueError(message)
            if (
                observe is None
                or observe.kind is not IntentKind.OBSERVE
                or observe.continuation_id != key
            ):
                message = "lifecycle continuation requires its owned observe intent"
                raise ValueError(message)
            resume = self.intents.get(f"{key}/resume")
            if resume is not None and (
                not continuation.settled
                or resume.kind is not IntentKind.RESUME
                or resume.continuation_id != key
            ):
                message = "lifecycle continuation resume requires settled dependencies and matching identity"
                raise ValueError(message)
            if key != continuation.continuation_id:
                message = "lifecycle.continuations key differs from continuation_id"
                raise ValueError(message)
        return self


def _validate_park_authority(state: LifecycleState, continuation: EvaluationContinuation) -> None:
    if continuation.status is not ContinuationStatus.PARKED:
        return
    park = state.intents.get(continuation.park_operation_id or "")
    if (
        park is None
        or park.kind is not IntentKind.PARK
        or (park.scope_id, park.generation) != (continuation.scope_id, continuation.generation)
    ):
        message = "lifecycle parked continuation requires its owned park_operation_id"
        raise ValueError(message)


class PrepareIntent(BaseModel):
    """Record intent before any external effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: LifecycleIntent

    @model_validator(mode="after")
    def _prepared_stage(self) -> PrepareIntent:
        if self.intent.stage is not IntentStage.PREPARED:
            message = "prepare intent.stage must be prepared"
            raise ValueError(message)
        return self


class DispatchIntent(BaseModel):
    """Authorize dispatch after preparation has been committed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str


class CompleteIntent(BaseModel):
    """Acknowledge a reconciled effect in the settlement transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str
    resume_revision: str | None = None


class BlockIntent(BaseModel):
    """Fence an effect whose external acceptance cannot be inspected."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str


class RecoveryStarted(BaseModel):
    """Replay unfinished intents before normal admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)


type LifecycleEvent = (
    PrepareIntent | DispatchIntent | CompleteIntent | BlockIntent | RecoveryStarted
)


def step(
    state: LifecycleState, event: LifecycleEvent
) -> tuple[LifecycleState, tuple[LifecycleRequest, ...]]:
    """Reduce an input to a fresh ledger and requests requiring reconciliation.

    The caller commits the returned ledger before dispatching requests. Duplicate
    preparation, authorization and acknowledgement are harmless; a conflicting
    identity raises ValueError. Recovery replays requests, never ordinary work.
    """
    intents = dict(state.intents)
    requests: tuple[LifecycleRequest, ...] = ()
    match event:
        case PrepareIntent(intent=intent):
            existing = intents.get(intent.operation_id)
            if existing is not None:
                if existing.model_copy(update={"stage": intent.stage}) != intent:
                    message = f"conflicting lifecycle operation_id {intent.operation_id!r}"
                    raise ValueError(message)
            else:
                intents[intent.operation_id] = intent
        case DispatchIntent(operation_id=operation_id):
            intent = intents[operation_id]
            permitted = _requests(state, (intent,))
            if intent.stage not in {IntentStage.COMPLETED, IntentStage.BLOCKED} and permitted:
                requests = permitted
                intent = intent.model_copy(update={"stage": IntentStage.DISPATCHED})
                intents[operation_id] = intent

        case CompleteIntent(operation_id=operation_id, resume_revision=revision):
            intent = intents[operation_id]
            if intent.stage is not IntentStage.COMPLETED:
                intents[operation_id] = intent.model_copy(
                    update={
                        "stage": IntentStage.COMPLETED,
                        "resume_revision": revision or intent.resume_revision,
                    }
                )
        case BlockIntent(operation_id=operation_id):
            intents[operation_id] = _blocked(intents[operation_id])
        case RecoveryStarted():
            requests = _requests(
                state,
                tuple(
                    intent
                    for intent in intents.values()
                    if intent.stage in {IntentStage.PREPARED, IntentStage.DISPATCHED}
                ),
            )
    return state.model_copy(update={"intents": intents}), requests


def _requests(
    state: LifecycleState, intents: tuple[LifecycleIntent, ...]
) -> tuple[LifecycleRequest, ...]:
    requests: list[LifecycleRequest] = []
    for intent in intents:
        if intent.continuation_id is None:
            requests.append(intent)
            continue
        continuation = state.continuations[intent.continuation_id]
        if state.stopped or continuation.status is not ContinuationStatus.ACTIVE:
            continue
        if intent.kind is IntentKind.OBSERVE:
            requests.append(
                ObserveEvaluations(
                    operation_id=intent.operation_id, continuation=continuation, stage=intent.stage
                )
            )
        elif intent.kind is IntentKind.RESUME:
            requests.append(
                ResumeAgentTurn(
                    operation_id=intent.operation_id,
                    invocation_id=intent.operation_id,
                    continuation=continuation,
                    stage=intent.stage,
                )
            )
    return tuple(requests)


def _blocked(intent: LifecycleIntent) -> LifecycleIntent:
    if intent.stage is IntentStage.COMPLETED:
        return intent
    return intent.model_copy(update={"stage": IntentStage.BLOCKED})


def awaiting_evaluation(state: LifecycleState, scope_id: str, generation: int) -> bool:
    """Derive awaiting status from a yielded turn and its unfinished resume."""
    for continuation in state.continuations.values():
        if (continuation.scope_id, continuation.generation) != (scope_id, generation):
            continue
        if continuation.status is ContinuationStatus.CANCELLED:
            continue
        resume = state.intents.get(f"{continuation.continuation_id}/resume")
        if resume is None or resume.stage is not IntentStage.COMPLETED:
            return True
    return False


def withdrawing(state: LifecycleState, scope_id: str) -> bool:
    """Project withdrawal authority from the durable ledger."""
    return any(
        intent.scope_id == scope_id
        and intent.kind in {IntentKind.PARK, IntentKind.CANCEL}
        and intent.stage is not IntentStage.COMPLETED
        for intent in state.intents.values()
    )


__all__ = [
    "BlockIntent",
    "CompleteIntent",
    "ContinuationStatus",
    "DispatchIntent",
    "EvaluationContinuation",
    "EvaluationDependency",
    "EvaluationEvidenceId",
    "EvaluationOutcome",
    "IntentKind",
    "IntentStage",
    "LifecycleEvent",
    "LifecycleIntent",
    "LifecycleRequest",
    "LifecycleState",
    "ObserveEvaluations",
    "PrepareIntent",
    "RecoveryStarted",
    "ResumeAgentTurn",
    "awaiting_evaluation",
    "step",
    "withdrawing",
]
