"""Provider-neutral contracts for durable asynchronous operations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

type ConcurrencyKey = str


@dataclass(frozen=True, slots=True)
class OperationPolicy:
    """Resource-neutral bounds for one coordinator."""

    max_await_timeout_s: float | None = None
    max_in_flight: int | None = None
    cancellation_timeout_s: float | None = None

    def __post_init__(self) -> None:
        """Reject nonsensical lifecycle bounds."""
        if self.max_await_timeout_s is not None and (
            isinstance(self.max_await_timeout_s, bool)
            or not math.isfinite(self.max_await_timeout_s)
            or self.max_await_timeout_s <= 0
        ):
            raise InvalidOperationPolicyError("max_await_timeout_s")
        if self.max_in_flight is not None and (
            isinstance(self.max_in_flight, bool)
            or not isinstance(self.max_in_flight, int)
            or self.max_in_flight <= 0
        ):
            raise InvalidOperationPolicyError("max_in_flight")
        if self.cancellation_timeout_s is not None and (
            isinstance(self.cancellation_timeout_s, bool)
            or not math.isfinite(self.cancellation_timeout_s)
            or self.cancellation_timeout_s <= 0
        ):
            raise InvalidOperationPolicyError("cancellation_timeout_s")


class InvalidOperationPolicyError(ValueError):
    """A coordinator bound is invalid."""

    def __init__(self, field: str) -> None:
        requirement = (
            "a positive integer" if field == "max_in_flight" else "a positive finite number"
        )
        super().__init__(f"{field} must be {requirement}")


class OperationState(StrEnum):
    """Durable lifecycle state."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        """Return whether no further transition is allowed."""
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELED,
            self.INTERRUPTED,
        }


class OperationRequest(BaseModel):
    """Opaque work plus the key that serializes related operations."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: str = Field(min_length=1)
    concurrency_key: ConcurrencyKey = Field(min_length=1)
    payload: JsonValue


class OperationHandle(BaseModel):
    """Authoritative durable operation snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request: OperationRequest
    state: OperationState
    result: JsonValue | None = None
    result_present: bool = False
    failure: str | None = None
    revision: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="before")
    @classmethod
    def _infer_legacy_nonnull_result_presence(cls, value: object) -> object:
        """Read records written before explicit JSON-null result presence."""
        if (
            isinstance(value, dict)
            and "result_present" not in value
            and value.get("state") == OperationState.SUCCEEDED
            and value.get("result") is not None
        ):
            return {**value, "result_present": True}
        return value

    @model_validator(mode="after")
    def _terminal_fields_match_state(self) -> OperationHandle:
        if self.state is OperationState.SUCCEEDED and not self.result_present:
            raise ValueError("succeeded operation requires a result")  # noqa: TRY003  # lint-waiver: LW-930008 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if self.state is OperationState.FAILED and not self.failure:
            raise ValueError("failed operation requires a failure")  # noqa: TRY003  # lint-waiver: LW-930009 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if self.state is not OperationState.FAILED and self.failure is not None:
            raise ValueError("only failed operation may contain a failure")  # noqa: TRY003  # lint-waiver: LW-930010 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        if self.state is not OperationState.SUCCEEDED and (
            self.result is not None or self.result_present
        ):
            raise ValueError("only succeeded operation may contain a result")  # noqa: TRY003  # lint-waiver: LW-930011 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return self


class OperationCompleted(BaseModel):
    """Bounded wait observed a terminal record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["completed"] = "completed"
    record: OperationHandle


class OperationTimedOut(BaseModel):
    """Bounded wait expired while the operation remains nonterminal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["timed_out"] = "timed_out"
    record: OperationHandle


OperationAwaitResult = Annotated[
    OperationCompleted | OperationTimedOut,
    Field(discriminator="kind"),
]


class OperationLifecycleEvent(BaseModel):
    """Concise fact emitted after an authoritative state write."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operation_id: str
    concurrency_key: str
    state: OperationState
    revision: Annotated[int, Field(ge=0)]


__all__ = [
    "ConcurrencyKey",
    "OperationAwaitResult",
    "OperationCompleted",
    "OperationHandle",
    "OperationLifecycleEvent",
    "OperationPolicy",
    "OperationRequest",
    "OperationState",
    "OperationTimedOut",
]
