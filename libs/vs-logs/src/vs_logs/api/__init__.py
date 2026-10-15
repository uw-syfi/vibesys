"""Public writer interface of vs_logs: durable log streams for humans, under typed owners.

A producer is handed a :class:`LogScope` and opens a :class:`LogStream` per command with
``scope.open(owner, command)``. The stream is a ``vs_sim.api.OutputSink``: the producer
writes its stdout and stderr chunks to it and knows nothing else. Leaving the stream's
``with`` block closes it with an outcome. :class:`LogStore` (``FileLogStore`` for a run's
log directory, ``MemoryLogStore`` as the Fake) is the wiring object: it is a scope, adopts
files someone else writes, and caps each stream (:class:`LogLimits`), evicting its oldest
segments while every retained byte keeps its offset. :func:`follow` copies a growing
``ByteSource`` (a remote or container file) into a sink.

Logs are a channel for humans only. Product code never reads a log back for facts; the
read side is the separate ``vs_logs.read`` module, which only the host helper may import.
"""

from vs_logs._core import DEFAULT_MAX_BYTES, LogLimits
from vs_logs._follow import FollowEnd, FollowResult, follow
from vs_logs._records import StreamOutcome
from vs_logs._store import (
    AdoptedLog,
    FileLogStore,
    LogScope,
    LogStore,
    LogStream,
    MemoryLogStore,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "AdoptedLog",
    "FileLogStore",
    "FollowEnd",
    "FollowResult",
    "LogLimits",
    "LogScope",
    "LogStore",
    "LogStream",
    "MemoryLogStore",
    "StreamOutcome",
    "follow",
]
