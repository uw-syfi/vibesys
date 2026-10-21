"""Bounded append-only run.log files, with offset-based reads and explicit stop reasons."""

from vs_runlog._file import FileRunLogWriter, RunLogReader
from vs_runlog._fold import fold_skeleton
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame
from vs_runlog._legacy import LegacyRunLogTee

__all__ = [
    "FileRunLogWriter",
    "LegacyRunLogTee",
    "ReadStop",
    "RunLogReader",
    "RunLogStopReason",
    "encode_frame",
    "fold_skeleton",
]
