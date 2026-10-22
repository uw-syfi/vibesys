"""Saved-run discovery projection and idempotent import through the transport owner.

The project catalog supplies metadata; this module never derives run facts.
``page_saved_runs`` fences pages by that catalog and publishes readable runs in
its existing host index. ``without_dashboards`` preserves the helper projection.
"""

from __future__ import annotations

import hashlib
from importlib import import_module
from typing import TYPE_CHECKING

from server.host_api import RunDigest

if TYPE_CHECKING:
    from pathlib import Path

    from server.host_api import RunList
    from vs_project.api import Project
    from vs_proto.api import host_payloads as pb
    from vs_sim.api import SleepingClock, Threads

# Like the helper's _wire boundary, native dependencies load only on a native
# request; the legacy dashboard projection must keep entrypoint imports light.
__all__ = ["page_saved_runs", "without_dashboards"]


def without_dashboards(listing: RunList) -> RunList:
    """Preserve the legacy omission for clients that did not request dashboards."""
    return listing.model_copy(
        update={
            "runs": tuple(
                run.model_copy(update={"digest": run.digest.model_copy(update={"dashboard": None})})
                if isinstance(run.digest, RunDigest)
                else run
                for run in listing.runs
            )
        }
    )


def _import(project: Project, run: pb.SavedRun, clock: SleepingClock, threads: Threads) -> None:
    transport = import_module("vibesys.api.transport")
    project_api = import_module("vs_project.api")
    pb = import_module("vs_proto.api").host_payloads
    assert_run_unlocked = import_module("vs_runlog.api").assert_run_unlocked

    path = project.run_log_path_for(project.root, run.run_id)
    try:
        assert_run_unlocked(path)
    except RuntimeError:
        if path.is_file():
            project.publish_run_for(project.root, run.run_id)
            run.imported = pb.SavedRunImport.AVAILABLE
        else:
            run.imported = pb.SavedRunImport.UNAVAILABLE
            run.detail = "Pause this run before importing its history"
        return
    try:
        result = transport.convert_run(project, run.run_id, clock=clock, threads=threads)
    except (OSError, ValueError, TypeError, project_api.ProjectError) as error:
        run.imported = pb.SavedRunImport.UNAVAILABLE
        run.detail = str(error)
        return
    run.imported = (
        pb.SavedRunImport.AVAILABLE
        if result.status
        in {transport.ConversionStatus.CONVERTED, transport.ConversionStatus.EXISTS}
        else pb.SavedRunImport.UNAVAILABLE
    )
    run.detail = result.reason


def page_saved_runs(
    request: pb.ListSavedRunsRequest,
    root: Path,
    runs: list[pb.SavedRun],
    *,
    clock: SleepingClock,
    threads: Threads,
) -> pb.ListSavedRunsResponse:
    """Return at most 200 rows and 1 MiB; refuse continuation after catalog drift.

    Import preserves original journals and positional recovery state. A refused
    import stays in the listing with its reason, rather than disappearing.
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
        _import(project, run, clock, threads)
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
