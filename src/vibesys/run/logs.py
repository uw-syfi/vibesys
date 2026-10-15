"""The run's log store: its streams, each under a :data:`~vibesys.log_owners.LogOwner`.

Producers get the store as a ``vs_logs.api.LogScope[LogOwner]`` and only write; nothing
in the product reads a log back. :func:`open_run_logs` is the one place a run opens it:
run assembly calls it once, and passes the scope it returns to every producer.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from vibesys.log_owners import LOG_OWNER, LogOwner, RunNarration
from vs_logs.api import FileLogStore, LogLimits, LogScope, MemoryLogStore, StreamOutcome
from vs_project.api import Project

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from vs_logs.api import NarrationHandler
    from vs_sim.api import Threads

RUN_LOG_OWNER = LOG_OWNER
"""The owner adapter a reader of a run's log store decodes owners with."""

_STREAMS_DIRECTORY = "streams"

NARRATION_COMMAND = "vibesys"
"""The command a run's narration stream records: the run's own process."""

UNAVAILABLE_LIMITS = LogLimits(max_bytes=1 << 16, segment_bytes=1 << 14)
"""Per-stream bytes kept in memory when the run's store cannot be opened on disk."""


def open_run_log_store(
    project_root: Path, run_id: str, *, threads: Threads, limits: LogLimits | None = None
) -> FileLogStore[LogOwner]:
    """Open the log store in ``run_id``'s log directory, closing streams a crash left open."""
    directory = Project.log_directory_for(project_root, run_id) / _STREAMS_DIRECTORY
    return FileLogStore(directory, LOG_OWNER, threads=threads, limits=limits)


@contextmanager
def open_run_logs(
    project_root: Path,
    run_id: str,
    *,
    threads: Threads,
    warn: Callable[[str], object],
    narration: NarrationHandler | None,
) -> Iterator[LogScope[LogOwner]]:
    """Open the run's log store and, with ``narration``, its narration stream.

    Inside the block the narration stream is attached to ``narration``; leaving it stops
    the handler and ends the stream. When the store cannot be opened, the run goes on:
    ``warn`` says so, producers write to a bounded in-memory store nobody reads, and
    ``narration`` is closed, so records reach only what else handles them (stderr).
    """
    try:
        store = open_run_log_store(project_root, run_id, threads=threads)
        stream = None if narration is None else store.open(RunNarration(), NARRATION_COMMAND)
    except OSError as exc:
        warn(
            f"vibesys: the run's log streams are unavailable ({exc}); "
            "its narration reaches only stderr.\n"
        )
        if narration is not None:
            narration.close()
        yield MemoryLogStore(LOG_OWNER, threads=threads, limits=UNAVAILABLE_LIMITS)
        return
    if narration is None or stream is None:
        yield store
        return
    narration.attach(stream)
    try:
        yield store
    finally:
        narration.close()
        stream.close(StreamOutcome.ENDED)
