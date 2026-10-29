"""Saved-run discovery: the project catalog's metadata, and whether each run has a typed log.

The project catalog supplies metadata; this module never derives run facts. ``page_saved_runs``
fences pages by that catalog and reads no journal. A run kept in the old format is listed as such
until it is migrated (``vibesys host convert-runs``). This command module loads only for
saved-run discovery.
"""

from __future__ import annotations

import hashlib
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import Project
    from vs_proto.api import host_payloads as pb

__all__ = ["page_saved_runs"]


def _availability(project: Project, run: pb.SavedRun) -> None:
    """A run is readable when it has a typed log; any other run is in the old format."""
    pb = import_module("vs_proto.api").host_payloads
    if project.run_log_path_for(project.root, run.run_id).is_file():
        run.imported = pb.SavedRunImport.AVAILABLE
    else:
        run.imported = pb.SavedRunImport.UNAVAILABLE
        run.detail = "old format"


def page_saved_runs(
    request: pb.ListSavedRunsRequest,
    root: Path,
    runs: list[pb.SavedRun],
) -> pb.ListSavedRunsResponse:
    """Return at most 200 rows and 1 MiB; refuse continuation after catalog drift.

    Listing checks one file per run. A run in the old format stays listed, labelled as such.
    """
    project_type = import_module("vs_project.api").Project
    pb = import_module("vs_proto.api").host_payloads

    digest = hashlib.sha256(str(root).encode())
    for run in runs:
        encoded = run.to_binary()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    revision = digest.hexdigest()
    if request.revision and request.revision != revision:
        message = "revision: saved-run catalog changed; restart the listing"
        raise ValueError(message)
    if request.offset > len(runs):
        message = "offset: exceeds saved-run catalog"
        raise ValueError(message)
    response = pb.ListSavedRunsResponse(
        request_id=request.request_id, project_root=str(root), revision=revision
    )
    project = project_type.open(root)
    for index in range(request.offset, min(request.offset + 200, len(runs))):
        run = pb.SavedRun.from_binary(runs[index].to_binary())
        _availability(project, run)
        response.runs.append(run)
        if index + 1 < len(runs):
            response.next_offset = index + 1
        else:
            response.clear_field("next_offset")
        if len(response.to_binary()) > 1 << 20:
            response.runs.pop()
            response.next_offset = index
            if not response.runs:
                message = "saved run exceeds response byte budget"
                raise ValueError(message)
            break
    return response
