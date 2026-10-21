"""Bounded append-only run.log files, with offset-based reads and explicit stop reasons."""

from vs_runlog._file import FileRunContent, FileRunLogWriter, RunLogReader, install_run_log
from vs_runlog._fold import fold_skeleton
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame
from vs_runlog._lock import assert_run_unlocked

__all__ = [
    "FileRunContent",
    "FileRunLogWriter",
    "ReadStop",
    "RunLogReader",
    "RunLogStopReason",
    "assert_run_unlocked",
    "encode_frame",
    "fold_skeleton",
    "install_run_log",
]
