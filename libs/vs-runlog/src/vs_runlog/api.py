"""Bounded append-only run.log files, with offset-based reads and explicit stop reasons."""

from vs_runlog._file import FileRunLogWriter, RunLogReader
from vs_runlog._framing import MAGIC, MAX_RECORD_BYTES, ReadStop, StopReason, encode_frame

__all__ = [
    "MAGIC",
    "MAX_RECORD_BYTES",
    "FileRunLogWriter",
    "ReadStop",
    "RunLogReader",
    "StopReason",
    "encode_frame",
]
