"""The read side of vs_logs: list a store's streams and read bounded chunks of one.

Only the host helper imports this module (``tach.toml`` exposes it to nothing else):
logs are for humans, and product code never reads them back for facts.

:func:`open_log_directory` reads a store any process writes; :func:`reader_of` reads the
store an in-process :class:`~vs_logs.api.LogStore` writes. A read starts at
:class:`FromOffset` or at :class:`Tail` and returns at most ``max_bytes`` as a
:class:`LogChunk`, labeled with the pipe that wrote each span and flagged
``truncated_before`` (asked-for bytes were evicted), ``eof`` and ``closed``. Every byte is
read through ``vs_sim.api.ByteSource``, so the reader never assumes local files.
"""

from vs_logs._core import FromOffset, PipeSpan, ReadStart, Tail
from vs_logs._reader import (
    LogChunk,
    LogReader,
    StreamInfo,
    UnknownStreamError,
    open_log_directory,
    reader_of,
)

__all__ = [
    "FromOffset",
    "LogChunk",
    "LogReader",
    "PipeSpan",
    "ReadStart",
    "StreamInfo",
    "Tail",
    "UnknownStreamError",
    "open_log_directory",
    "reader_of",
]
