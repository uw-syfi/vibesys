"""Durable input occurrences, invocation reservations and terminal receipts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    ContractValidationError,
    Count,
    InputId,
    InvocationRef,
    ItemId,
    Observation,
    Scope,
    Seconds,
    Value,
)


class InputMode(StrEnum):
    """Declared delivery mode; interrupt metadata grants no refund authority."""

    NEXT_TURN = "next-turn"
    INTERRUPT = "interrupt"


class ScopeInputTarget(Value):
    """Input for a scope, permitted before any invocation exists."""

    kind: Literal["scope"] = "scope"
    scope: Scope


class ItemInputTarget(Value):
    """Input for an item, permitted before any invocation exists."""

    kind: Literal["item"] = "item"
    item_id: ItemId


class InvocationInputTarget(Value):
    """Input for one exact invocation, never silently retargeted."""

    kind: Literal["invocation"] = "invocation"
    invocation: InvocationRef


type InputTarget = Annotated[
    ScopeInputTarget | ItemInputTarget | InvocationInputTarget,
    Field(discriminator="kind"),
]


class SessionInput(Value):
    """One input occurrence, even when another occurrence has identical content.

    Pending inputs reserve in sequence then stable-ID order before dispatch.
    Migration must supply explicit occurrence mapping for legacy artifact-only
    notes; content digest does not identify an occurrence.
    """

    input_id: InputId
    target: InputTarget
    artifact: ArtifactRef
    received_at: Seconds
    sequence: Count
    mode: InputMode = InputMode.NEXT_TURN


class InputDropReason(StrEnum):
    """Closed reasons for terminal input disposal."""

    OWNER_TERMINAL = "owner-terminal"
    OWNER_CANCELLED = "owner-cancelled"
    RUN_TERMINAL = "run-terminal"
    INVOCATION_TERMINAL = "invocation-terminal"


class InputDelivered(Value):
    """Receipt requiring confirmed acceptance of the exact reserved invocation.

    Local task cancellation never proves delivery. Delivery and disposal are
    mutually exclusive terminal receipts and replay cannot issue either twice.
    """

    kind: Literal["input_delivered"] = "input_delivered"
    input_id: InputId
    invocation: InvocationRef
    observation: Observation

    @model_validator(mode="after")
    def confirmed_acceptance(self) -> InputDelivered:
        """Delivery receipts require positive acceptance for the exact generation."""
        if not self.observation.accepted:
            raise ContractValidationError("observation.accepted", "delivery requires acceptance")
        if self.observation.scope.generation != self.invocation.generation:
            raise ContractValidationError("observation.scope.generation", "differs from invocation")
        return self


class InputDropped(Value):
    """One terminal disposal receipt after the target can no longer consume input."""

    kind: Literal["input_dropped"] = "input_dropped"
    input_id: InputId
    target: InputTarget
    reason: InputDropReason
    at: Seconds


type InputReceipt = Annotated[InputDelivered | InputDropped, Field(discriminator="kind")]


class InputRecord(Value):
    """Sole reservation authority for an occurrence and its terminal receipt.

    Unknown acceptance preserves reservation and payload. Positive unaccepted
    abandonment returns scope/item inputs to pending. Park preserves pending
    inputs; terminal retirement drops eligible pending inputs once.
    """

    input: SessionInput
    reserved_to: InvocationRef | None = None
    receipt: InputReceipt | None = None

    @model_validator(mode="after")
    def receipt_correspondence(self) -> InputRecord:
        """Reject receipts and reservations for a different input or invocation."""
        if self.receipt is not None and self.receipt.input_id != self.input.input_id:
            raise ContractValidationError("receipt.input_id", "does not match input.input_id")
        if isinstance(self.input.target, InvocationInputTarget) and (
            self.reserved_to is not None and self.reserved_to != self.input.target.invocation
        ):
            raise ContractValidationError("reserved_to", "does not match invocation target")
        if isinstance(self.receipt, InputDelivered) and self.receipt.invocation != self.reserved_to:
            raise ContractValidationError("receipt.invocation", "does not match reserved_to")
        if isinstance(self.receipt, InputDropped) and self.receipt.target != self.input.target:
            raise ContractValidationError("receipt.target", "does not match input.target")
        return self
