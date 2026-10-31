"""Pure recovery rule: fold a run's log into core state, and decide what the runner loop does next.

Design: ``docs/contributing/run-recovery.md``. Nothing here performs I/O.
"""

from vs_core._actions import (
    Action,
    Cancel,
    End,
    Run,
    RunRest,
    Settle,
    Wait,
    at_rest,
    interruption_of,
)
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
    Interruption,
    InterruptPolicy,
    StepAttempts,
    StepResult,
    StepsState,
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
    "Interruption",
    "Run",
    "RunRest",
    "Settle",
    "StepAttempts",
    "StepResult",
    "StepsState",
    "Wait",
    "at_rest",
    "decide",
    "finished_message",
    "fold",
    "input_message",
    "interrupted_message",
    "interruption_of",
    "started_message",
]
