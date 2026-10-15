"""The run's log store: its streams, each under a :data:`~vibesys.log_owners.LogOwner`.

Producers get the store as a ``vs_logs.api.LogScope[LogOwner]`` and only write; nothing
in the product reads a log back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.log_owners import LOG_OWNER, LogOwner
from vs_logs.api import FileLogStore, LogLimits
from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import Threads

RUN_LOG_OWNER = LOG_OWNER
"""The owner adapter a reader of a run's log store decodes owners with."""

_STREAMS_DIRECTORY = "streams"


def open_run_log_store(
    project_root: Path, run_id: str, *, threads: Threads, limits: LogLimits | None = None
) -> FileLogStore[LogOwner]:
    """Open the log store in ``run_id``'s log directory, closing streams a crash left open."""
    directory = Project.log_directory_for(project_root, run_id) / _STREAMS_DIRECTORY
    return FileLogStore(directory, LOG_OWNER, threads=threads, limits=limits)
