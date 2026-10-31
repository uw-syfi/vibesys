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
from vs_core._decide import (
    Folded,
    apply_record,
    decide,
    fold,
    fold_run,
    may_start,
    starts_run,
)
from vs_core._search_events import Translated
from vs_core._search_log import (
    CHECKPOINT_GROWTH,
    Advancing,
    Progress,
    advance_search,
    checkpoint_search,
    restore,
)
from vs_core._step import advance_run_time
from vs_core._step_attempts import open_step
from vs_core._step_log import (
    SavedRun,
    decode_seed,
    decode_snapshot,
    ended_message,
    finished_message,
    input_message,
    interrupted_message,
    seed_message,
    snapshot_message,
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
    OwnerEvent,
    StepAttempts,
    StepResult,
    StepsState,
)

__all__ = [
    "CHECKPOINT_GROWTH",
    "Action",
    "Advancing",
    "Cancel",
    "CancelReason",
    "Cancelled",
    "End",
    "Failed",
    "Finished",
    "Folded",
    "InterruptPolicy",
    "Interruption",
    "OpenStep",
    "OwnerEvent",
    "Progress",
    "Run",
    "RunRest",
    "SavedRun",
    "Settle",
    "StepAttempts",
    "StepEffect",
    "StepResult",
    "StepsState",
    "Translated",
    "Wait",
    "advance_run_time",
    "advance_search",
    "apply_record",
    "at_rest",
    "checkpoint_search",
    "decide",
    "decode_seed",
    "decode_snapshot",
    "ended_message",
    "finished_message",
    "fold",
    "fold_run",
    "input_message",
    "interrupted_message",
    "interruption_of",
    "may_start",
    "open_step",
    "request_effect",
    "request_interrupt_policy",
    "restore",
    "seed_message",
    "snapshot_message",
    "started_message",
    "starts_run",
]
