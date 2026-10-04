"""Queue, admission, resource pools and global charge contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .common import (
    AttemptId,
    AttemptRef,
    Count,
    DecisionId,
    Generation,
    ItemId,
    PoolId,
    Seconds,
    Value,
)


class AttemptRequest(Value):
    """Attempt request lifecycle contract."""

    decision_id: DecisionId
    attempt_id: AttemptId
    item_id: ItemId
    generation: Generation
    admission_charge: Count
    pools: tuple[PoolId, ...] = ()


class Slot(Value):
    """Slot lifecycle contract."""

    attempt: AttemptRef
    pools: tuple[PoolId, ...] = ()


class SchedulingState(Value):
    """Scheduling state lifecycle contract."""

    queue: tuple[AttemptRequest, ...] = ()
    slots: tuple[Slot, ...] = ()
    charged: Count = 0
    refunded: Count = 0
    admission_closed: bool = False


class SchedulingView(Value):
    """Scheduling view lifecycle contract."""

    queue: tuple[AttemptRequest, ...]
    slots: tuple[Slot, ...]
    available_tokens: Count
    charged: Count
    refunded: Count
    admission_closed: bool


class AttemptRequested(Value):
    """Attempt requested lifecycle contract."""

    kind: Literal["attempt_requested"] = "attempt_requested"
    request: AttemptRequest


class AttemptReady(Value):
    """Attempt ready lifecycle contract."""

    kind: Literal["attempt_ready"] = "attempt_ready"
    attempt: AttemptRef


class SlotReleased(Value):
    """Slot released lifecycle contract."""

    kind: Literal["slot_released"] = "slot_released"
    attempt: AttemptRef


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
    request: AttemptRequest


class CloseAdmission(Value):
    """Close admission lifecycle contract."""

    kind: Literal["close_admission"] = "close_admission"


class RunDrained(Value):
    """Run drained lifecycle contract."""

    kind: Literal["run_drained"] = "run_drained"


# Admission/closure are internal signals, never shell I/O.
type SchedulingEvent = Annotated[
    AttemptRequested | AttemptReady | SlotReleased | ClockAdvanced | AdmissionControl,
    Field(discriminator="kind"),
]
