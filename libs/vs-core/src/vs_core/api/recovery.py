"""Pure recovery rule: the actions core asks of the runner loop and how a step ends.

Design: ``docs/contributing/run-recovery.md``. Nothing here performs I/O.
"""

from vs_core._actions import Action, Cancel, End, Run, Settle, Wait
from vs_core.types.steps import (
    Cancelled,
    CancelReason,
    Failed,
    Finished,
    InterruptPolicy,
    StepResult,
)

__all__ = [
    "Action",
    "Cancel",
    "CancelReason",
    "Cancelled",
    "End",
    "Failed",
    "Finished",
    "InterruptPolicy",
    "Run",
    "Settle",
    "StepResult",
    "Wait",
]
