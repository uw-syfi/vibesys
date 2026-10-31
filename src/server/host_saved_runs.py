"""Saved-run discovery: the project catalog's metadata, and whether each run has a typed log.

The project catalog supplies metadata; this module never derives run facts and reads no journal.
A run kept in the old format is listed as such until it is migrated
(``vibesys host convert-runs``). This command module loads only for saved-run discovery.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from server.host_run_reads import ProjectRun
    from vs_proto.api import host_payloads as pb

__all__ = ["saved_run_rows"]


def saved_run_rows(root: Path, runs: Iterable[ProjectRun]) -> Iterator[pb.SavedRun]:
    """Each of ``runs`` as a row, in the order they come.

    Listing checks one file per run: a run is readable when it has a typed log, and any other
    run stays listed, labelled as being in the old format.
    """
    wire = import_module("vs_proto.api")
    pb = wire.host_payloads
    project = import_module("vs_project.api").Project
    for run in runs:
        readable = project.run_log_path_for(root, run.run_id).is_file()
        row = pb.SavedRun(
            run_id=run.run_id,
            task=run.task,
            display_name=run.display_name,
            created_at=wire.Timestamp.from_datetime(run.created_at),
            imported=pb.SavedRunImport.AVAILABLE if readable else pb.SavedRunImport.UNAVAILABLE,
        )
        if not readable:
            row.detail = "old format"
        yield row
