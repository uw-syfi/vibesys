"""Pure recovery rule: fold a run's log into core state, and decide what the runner loop does next.

Design: ``docs/contributing/run-recovery.md``. Nothing here performs I/O.
"""

from vs_core._actions import (
    Action,
    Cancel,
    End,
    OpenStep,
    Run,
    RunRest,
    Settle,
    Wait,
    at_rest,
    interruption_of,
)
from vs_core._decide import decide, fold, may_start, start_due
from vs_core._refusal import RecordRefusal, RecordRefusedError, RefusalReason
from vs_core._step_attempts import open_step
from vs_core._step_log import (
    ended_message,
    finished_message,
    input_message,
    interrupted_message,
    started_message,
)
from vs_core.types.records import StepEffect, request_effect, request_interrupt_policy
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
    "OpenStep",
    "RecordRefusal",
    "RecordRefusedError",
    "RefusalReason",
    "Run",
    "RunRest",
    "Settle",
    "StepAttempts",
    "StepEffect",
    "StepResult",
    "StepsState",
    "Wait",
    "at_rest",
    "decide",
    "ended_message",
    "finished_message",
    "fold",
    "input_message",
    "interrupted_message",
    "interruption_of",
    "may_start",
    "open_step",
    "request_effect",
    "request_interrupt_policy",
    "start_due",
    "started_message",
]
