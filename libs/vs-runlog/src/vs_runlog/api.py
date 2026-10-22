"""Bounded append-only run.log files, with offset-based reads and explicit stop reasons."""

from vs_runlog._atomic import FileSync, OsFileSync
from vs_runlog._epoch import CurrentEpoch, current_epoch
from vs_runlog._file import (
    FileRunContent,
    FileRunLogWriter,
    RunLogReader,
    install_run_log,
    open_run_log,
)
from vs_runlog._fold import fold_skeleton
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame
from vs_runlog._lock import RunLaunchLockedError, RunLock, assert_run_unlocked, exclusive_run_launch

__all__ = [
    "CurrentEpoch",
    "FileRunContent",
    "FileRunLogWriter",
    "FileSync",
    "OsFileSync",
    "ReadStop",
    "RunLaunchLockedError",
    "RunLock",
    "RunLogReader",
    "RunLogStopReason",
    "assert_run_unlocked",
    "current_epoch",
    "encode_frame",
    "exclusive_run_launch",
    "fold_skeleton",
    "install_run_log",
    "open_run_log",
]
