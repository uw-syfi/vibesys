"""Bounded append-only run.log files, with offset-based reads and explicit stop reasons."""

from vs_runlog._atomic import FakeFileSync, FileSync, OsFileSync
from vs_runlog._epoch import CurrentEpoch, current_epoch, epoch_has_ended
from vs_runlog._file import (
    FileRunContent,
    FileRunLogWriter,
    RunLogReader,
    install_run_log,
    open_run_log,
)
from vs_runlog._fold import fold_skeleton
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame
from vs_runlog._lock import (
    RunLaunchLockedError,
    RunLock,
    WriterIdentityState,
    assert_run_unlocked,
    exclusive_run_launch,
    observe_writer_identity,
    writer_identity_path,
)
from vs_runlog._observe import HeadObserver, RunLogDamagedError, read_current_epoch, read_run_head
from vs_runlog._upgrade import upgrade_skeleton

__all__ = [
    "CurrentEpoch",
    "FakeFileSync",
    "FileRunContent",
    "FileRunLogWriter",
    "FileSync",
    "HeadObserver",
    "OsFileSync",
    "ReadStop",
    "RunLaunchLockedError",
    "RunLock",
    "RunLogDamagedError",
    "RunLogReader",
    "RunLogStopReason",
    "WriterIdentityState",
    "assert_run_unlocked",
    "current_epoch",
    "encode_frame",
    "epoch_has_ended",
    "exclusive_run_launch",
    "fold_skeleton",
    "install_run_log",
    "observe_writer_identity",
    "open_run_log",
    "read_current_epoch",
    "read_run_head",
    "upgrade_skeleton",
    "writer_identity_path",
]
