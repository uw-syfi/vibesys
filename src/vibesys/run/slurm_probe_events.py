"""Project a local Slurm root's workspace-root probe onto the core event stream."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import CoreEventType, CoreEventWriter, EventStatus, WorkspaceRootProbeData
from vs_slurm.api import (
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeQueued,
)

if TYPE_CHECKING:
    from vs_slurm.api import WorkspaceRootProbeReport

__all__ = ["CoreWorkspaceRootProbeEvents"]


class CoreWorkspaceRootProbeEvents:
    """Publish the probe's reports on one run's semantic event stream; frontends word them.

    ``report`` is the run environment's ``workspace_root_probe`` sink. The probe already
    bounds how often it reports a queued job, so every report becomes one event.
    """

    def __init__(self, events: CoreEventWriter) -> None:
        """Route probe reports into ``events``."""
        self._events = events

    def report(self, report: WorkspaceRootProbeReport) -> None:
        """Publish one probe report as a ``workspace_root_probe`` event."""
        match report:
            case WorkspaceRootProbeQueued():
                status = EventStatus.ACTIVE
                data = WorkspaceRootProbeData(
                    state="queued",
                    job_id=report.job_id,
                    pending_reason=report.pending_reason,
                    waited_seconds=report.waited_seconds,
                    queue_timeout_seconds=report.queue_timeout_seconds,
                )
            case WorkspaceRootProbeProven():
                status, data = EventStatus.COMPLETED, WorkspaceRootProbeData(state="proven")
            case WorkspaceRootProbeFailed():
                status = EventStatus.FAILED
                data = WorkspaceRootProbeData(state="failed", failure=report.failure)
        self._events.emit(CoreEventType.WORKSPACE_ROOT_PROBE, status=status, data=data)
