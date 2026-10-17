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
    InputMode,
    InputTarget,
    InvocationInputTarget,
    InvocationRef,
    Observation,
    Scope,
    Seconds,
    Value,
)


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

    @model_validator(mode="after")
    def standing_outlives_one_turn(self) -> SessionInput:
        """Standing and interrupt input outlive one turn, so neither can name one invocation.

        Standing input is for every matching turn; interrupt input falls back to the next
        matching turn when the running one cannot take it.
        """
        if self.mode in (InputMode.STANDING, InputMode.INTERRUPT) and isinstance(
            self.target, InvocationInputTarget
        ):
            raise ContractValidationError(
                "mode", f"{self.mode.value} input is for a run, role, attempt or item"
            )
        return self


class InputDropReason(StrEnum):
    """Closed reasons for terminal input disposal."""

    OWNER_TERMINAL = "owner-terminal"
    OWNER_CANCELLED = "owner-cancelled"
    RUN_TERMINAL = "run-terminal"
    INVOCATION_TERMINAL = "invocation-terminal"
    WITHDRAWN = "withdrawn"
    """An operator withdrew standing guidance."""
    DELIVERY_UNKNOWN = "delivery-unknown"
    """No later turn can take the input, but a running turn's provider took it mid-turn and no
    turn end confirmed or refused it (its host died): the agent may have read it."""


class InputDelivered(Value):
    """Receipt requiring confirmed acceptance of the exact reserved invocation.

    Local task cancellation never proves delivery. Delivery and disposal are
    mutually exclusive terminal receipts and replay cannot issue either twice.
    """

    kind: Literal["input_delivered"] = "input_delivered"
    input_id: InputId
    invocation: InvocationRef
    observation: Observation
    mid_turn: bool = False
    """The input joined the invocation's running turn instead of opening its next one."""

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


class MidTurnOffer(Value):
    """A pending interrupt-mode input and the running turn it can be offered to now."""

    input: SessionInput
    invocation: InvocationRef
    scope: Scope


class InputRecord(Value):
    """Sole reservation authority for an occurrence and its terminal receipt.

    Unknown acceptance preserves reservation and payload. Positive unaccepted
    abandonment returns scope/item inputs to pending. Park preserves pending
    inputs; terminal retirement drops eligible pending inputs once. After
    ownership drains, run finalization records a RUN_TERMINAL drop for every
    remaining occurrence before RunEnded publication. Existing delivered or
    dropped receipts remain unchanged; finalization cannot issue a second receipt.
    """

    input: SessionInput
    reserved_to: InvocationRef | None = None
    offered_to: InvocationRef | None = None
    """The running turn an interrupt-mode input was offered to, before any provider call.

    Never set together with ``reserved_to``: an offered input waits for that turn's end
    (delivered with it, or returned to pending) and no next turn reserves it meanwhile.
    """
    taken_by: InvocationRef | None = None
    """The running turn, the one it is offered to, whose provider took the input mid-turn.

    A fact of that one offer: the input joins that turn only if the turn ends accepted while
    the offer stands. Every release of the offer clears it.
    """
    possibly_read: bool = False
    """A turn whose provider took the input ended without delivering or refusing it.

    The agent may have read it, so a later drop records ``DELIVERY_UNKNOWN`` instead of saying
    it reached no turn. It stays set when the input is offered or reserved again.
    """
    receipt: InputReceipt | None = None

    @model_validator(mode="after")
    def offer_correspondence(self) -> InputRecord:
        """An offer, and a take of it, are of one interrupt-mode input by the turn holding it."""
        if self.offered_to is not None:
            if self.reserved_to is not None:
                raise ContractValidationError("offered_to", "an offered input is not reserved")
            if self.input.mode is not InputMode.INTERRUPT:
                raise ContractValidationError(
                    "offered_to", "only an interrupt-mode input is offered"
                )
        if (self.taken_by is not None or self.possibly_read) and (
            self.input.mode is not InputMode.INTERRUPT
        ):
            raise ContractValidationError(
                "taken_by", "only an interrupt-mode input is taken mid-turn"
            )
        if self.taken_by is not None and self.taken_by != self.offered_to:
            raise ContractValidationError("taken_by", "a turn takes only the offer it holds")
        if (
            isinstance(self.receipt, InputDelivered)
            and self.receipt.mid_turn
            and self.taken_by != self.receipt.invocation
        ):
            raise ContractValidationError(
                "receipt.mid_turn", "only the turn that took an offer gets it mid-turn"
            )
        return self

    @model_validator(mode="after")
    def receipt_correspondence(self) -> InputRecord:
        """Reject receipts and reservations for a different input or invocation."""
        if self.receipt is not None and self.receipt.input_id != self.input.input_id:
            raise ContractValidationError("receipt.input_id", "does not match input.input_id")
        if isinstance(self.input.target, InvocationInputTarget) and (
            self.reserved_to is not None and self.reserved_to != self.input.target.invocation
        ):
            raise ContractValidationError("reserved_to", "does not match invocation target")
        if isinstance(self.receipt, InputDelivered):
            holder = self.offered_to if self.receipt.mid_turn else self.reserved_to
            if self.receipt.invocation != holder:
                raise ContractValidationError(
                    "receipt.invocation",
                    "does not match offered_to"
                    if self.receipt.mid_turn
                    else "does not match reserved_to",
                )
        if isinstance(self.receipt, InputDropped) and self.receipt.target != self.input.target:
            raise ContractValidationError("receipt.target", "does not match input.target")
        return self
