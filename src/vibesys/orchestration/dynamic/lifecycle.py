"""Pure durable lifecycle transitions, persisted by the dynamic shell before effects.

``step`` never mutates its input. Unfinished requests retain stable identities
across replay; handlers acknowledge only after their idempotent effect finishes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentKind(StrEnum):
    """The lifecycle transitions supported by this repair."""

    PARK = "park"
    CANCEL = "cancel"
    INTERRUPT = "interrupt"
    TURN = "turn"
    REOPEN = "reopen"


class IntentStage(StrEnum):
    """Durable dispatch progress, including explicitly unresolved effects."""

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    BLOCKED = "blocked"


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

    @model_validator(mode="after")
    def _turn_identity(self) -> LifecycleIntent:
        if self.kind is IntentKind.TURN and self.invocation_id != self.operation_id:
            message = "turn invocation_id must equal operation_id"
            raise ValueError(message)
        if self.kind is not IntentKind.TURN and self.invocation_id is not None:
            message = "invocation_id belongs only to turn intents"
            raise ValueError(message)
        return self


class LifecycleState(BaseModel):
    """The intent ledger embedded in the atomic run envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    intents: dict[str, LifecycleIntent] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _operation_keys(self) -> LifecycleState:
        for key, intent in self.intents.items():
            if key != intent.operation_id:
                message = f"lifecycle.intents key {key!r} differs from operation_id {intent.operation_id!r}"
                raise ValueError(message)
        return self


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
) -> tuple[LifecycleState, tuple[LifecycleIntent, ...]]:
    """Reduce an input to a fresh ledger and requests requiring reconciliation.

    The caller commits the returned ledger before dispatching requests. Duplicate
    preparation, authorization and acknowledgement are harmless; a conflicting
    identity raises ValueError. Recovery replays requests, never ordinary work.
    """
    intents = dict(state.intents)
    requests: tuple[LifecycleIntent, ...] = ()
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
            if intent.stage not in {IntentStage.COMPLETED, IntentStage.BLOCKED}:
                intent = intent.model_copy(update={"stage": IntentStage.DISPATCHED})
                intents[operation_id] = intent
                requests = (intent,)
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
            requests = tuple(
                intent
                for intent in intents.values()
                if intent.stage in {IntentStage.PREPARED, IntentStage.DISPATCHED}
            )
    return LifecycleState(intents=intents), requests


def _blocked(intent: LifecycleIntent) -> LifecycleIntent:
    if intent.stage is IntentStage.COMPLETED:
        return intent
    return intent.model_copy(update={"stage": IntentStage.BLOCKED})


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
    "DispatchIntent",
    "IntentKind",
    "IntentStage",
    "LifecycleEvent",
    "LifecycleIntent",
    "LifecycleState",
    "PrepareIntent",
    "RecoveryStarted",
    "step",
    "withdrawing",
]
