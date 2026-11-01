"""The step runner interface and the loop that executes core's actions through it."""

from collections.abc import Callable

from vs_core.api.recovery import OpenStep
from vs_runtime._core_inputs import Controls, CoreInputs, SearchRun
from vs_runtime._fold_keeper import CACHE_EVERY, CacheUsed, FoldStart, start_fold
from vs_runtime._run_log_step_log import (
    RestAnnouncer,
    RunLogStepLog,
    RunLogUncertainError,
    SearchHearing,
)
from vs_runtime._saved_run_record import fold_log, restore_log, saved_run_in, snapshot_message
from vs_runtime._step_loop import (
    DispatchCapExceededError,
    Interrupted,
    Recorded,
    Stall,
    StallBoard,
    StallKind,
    StepLog,
    StepLoopStalledError,
    StepLoopTiming,
    StepLoopTimingError,
    StepRecord,
    run_step_loop,
)
from vs_runtime._step_runner import (
    Adopted,
    CancelSignal,
    Cleared,
    ClearUnavailableError,
    StepRunner,
)

type StepRunnerDecorator = Callable[[StepRunner], StepRunner]
"""Wraps the runner a run composes; a test places a hold or a fault at the seam every step crosses."""

__all__ = [
    "CACHE_EVERY",
    "Adopted",
    "CacheUsed",
    "CancelSignal",
    "ClearUnavailableError",
    "Cleared",
    "Controls",
    "CoreInputs",
    "DispatchCapExceededError",
    "FoldStart",
    "Interrupted",
    "OpenStep",
    "Recorded",
    "RestAnnouncer",
    "RunLogStepLog",
    "RunLogUncertainError",
    "SearchHearing",
    "SearchRun",
    "Stall",
    "StallBoard",
    "StallKind",
    "StepLog",
    "StepLoopStalledError",
    "StepLoopTiming",
    "StepLoopTimingError",
    "StepRecord",
    "StepRunner",
    "StepRunnerDecorator",
    "fold_log",
    "restore_log",
    "run_step_loop",
    "saved_run_in",
    "snapshot_message",
    "start_fold",
]
