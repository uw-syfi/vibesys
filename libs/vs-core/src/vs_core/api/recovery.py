"""Pure recovery rule: fold a run's log into core state, and decide what the runner loop does next.

Design: ``docs/contributing/run-recovery.md``. Nothing here performs I/O.
"""

from vs_core._actions import Action, Cancel, End, Run, Settle, Wait
from vs_core._decide import decide, fold
from vs_core._step_log import (
    finished_message,
    input_message,
    interrupted_message,
    started_message,
)
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
    "decide",
    "finished_message",
    "fold",
    "input_message",
    "interrupted_message",
    "started_message",
]
