"""Queue, admission, resource pools and global charge contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import (
    AttemptId,
    AttemptRef,
    ContractValidationError,
    Count,
    DecisionId,
    Generation,
    ItemId,
    PoolId,
    RequestId,
    Seconds,
    Value,
)


class AttemptRequest(Value):
    """Attempt request lifecycle contract."""

    kind: Literal["start"] = "start"
    decision_id: DecisionId
    attempt_id: AttemptId
    item_id: ItemId
    generation: Generation
    admission_charge: Count
    pools: tuple[PoolId, ...] = ()


class AttemptReopenRequest(Value):
    """FIFO reentry into capacity without an additional ADMISSION charge."""

    kind: Literal["reopen"] = "reopen"
    decision_id: DecisionId
    request_id: RequestId
    attempt: AttemptRef
    pools: tuple[PoolId, ...] = ()


type AdmissionRequest = Annotated[
    AttemptRequest | AttemptReopenRequest, Field(discriminator="kind")
]


class Slot(Value):
    """One occupancy episode, retaining capacity through cleanup.

    Charge-end and physical release are separate. Match attempt and admission
    identity so delayed old ready/end/release facts cannot affect reentry.
    """

    attempt: AttemptRef
    admission_id: DecisionId
    pools: tuple[PoolId, ...] = ()
    admitted_at: Seconds
    charge_ended_at: Seconds | None = None

    @model_validator(mode="after")
    def chronological_charge(self) -> Slot:
        """An occupancy interval cannot end before admission."""
        if self.charge_ended_at is not None and self.charge_ended_at < self.admitted_at:
            raise ContractValidationError("charge_ended_at", "precedes admitted_at")
        return self


class SchedulingState(Value):
    """Admission queue and capacity ownership; accounting derives from receipts.

    Reopen obeys ordinary FIFO, pools, exclusivity, pause and parallel bounds.
    released_slot_seconds accumulates a released episode's interval once.
    """

    queue: tuple[AdmissionRequest, ...] = ()
    slots: tuple[Slot, ...] = ()
    admission_closed: bool = False
    released_slot_seconds: Seconds = 0.0


class SchedulingView(Value):
    """Derived ADMISSION usage and occupancy telemetry.

    charged/refunded sum authoritative ADMISSION receipts. slot_seconds includes
    released intervals and ended held intervals; active_slot_seconds sums current
    time minus admission time for charge-running held intervals.
    """

    queue: tuple[AdmissionRequest, ...]
    slots: tuple[Slot, ...]
    available_tokens: Count
    charged: Count
    refunded: Count
    admission_closed: bool
    slot_seconds: Seconds
    active_slot_seconds: Seconds


class AttemptRequested(Value):
    """Attempt requested lifecycle contract."""

    kind: Literal["attempt_requested"] = "attempt_requested"
    request: AttemptRequest


class AttemptReady(Value):
    """Attempt ready lifecycle contract."""

    kind: Literal["attempt_ready"] = "attempt_ready"
    attempt: AttemptRef
    admission_id: DecisionId


class SlotReleased(Value):
    """Slot released lifecycle contract."""

    kind: Literal["slot_released"] = "slot_released"
    attempt: AttemptRef
    admission_id: DecisionId


class AttemptReopenRequested(Value):
    """Scheduling reentry request after exact parked-authority validation."""

    kind: Literal["attempt_reopen_requested"] = "attempt_reopen_requested"
    request: AttemptReopenRequest


class SlotChargeEnded(Value):
    """First closure ends telemetry charging while capacity remains occupied."""

    kind: Literal["slot_charge_ended"] = "slot_charge_ended"
    attempt: AttemptRef
    admission_id: DecisionId
    ended_at: Seconds


class RegisterAttempt(Value):
    """Scheduling asks the kernel to register a queued attempt before admission."""

    kind: Literal["register_attempt"] = "register_attempt"
    request: AttemptRequest


class QueueEntryRetired(Value):
    """Retire only the exact queued episode without inventing an occupied slot.

    Attempts B supplies the queued start/reopen decision identity. Scheduling
    ignores older parked admissions and never retires a later queue entry.
    """

    kind: Literal["queue_entry_retired"] = "queue_entry_retired"
    attempt: AttemptRef
    admission_id: DecisionId


class ClockAdvanced(Value):
    """Clock advanced lifecycle contract."""

    kind: Literal["clock_advanced"] = "clock_advanced"
    now_at: Seconds


class AdmissionControl(Value):
    """Admission control lifecycle contract."""

    kind: Literal["admission_control"] = "admission_control"
    action: Literal["pause", "resume", "drain", "cancel"]


class AdmitAttempt(Value):
    """Admit attempt lifecycle contract."""

    kind: Literal["admit_attempt"] = "admit_attempt"
    request: AdmissionRequest


class CloseAdmission(Value):
    """Close admission lifecycle contract."""

    kind: Literal["close_admission"] = "close_admission"


class RunDrained(Value):
    """Run drained lifecycle contract."""

    kind: Literal["run_drained"] = "run_drained"


class AdoptionFenceLifted(Value):
    """An adoption ended (verified or failed), so root-exclusive admission may proceed.

    Settlement emits it once, when the adoption fence lifts. It carries no facts:
    Scheduling re-evaluates its queue against the current state, so a duplicate is
    harmless.
    """

    kind: Literal["adoption_fence_lifted"] = "adoption_fence_lifted"


# Admission/closure are internal signals, never shell I/O.
type SchedulingEvent = Annotated[
    AttemptRequested
    | AttemptReopenRequested
    | AttemptReady
    | SlotReleased
    | SlotChargeEnded
    | QueueEntryRetired
    | ClockAdvanced
    | AdmissionControl
    | AdoptionFenceLifted,
    Field(discriminator="kind"),
]
