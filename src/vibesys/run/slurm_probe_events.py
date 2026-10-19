"""Project a local Slurm root's workspace-root probe onto the core event stream."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import CoreEventType, CoreEventWriter, EventStatus, WorkspaceRootProbeData
from vs_slurm.api import (
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeQueued,
    WorkspaceRootProbeRetrying,
)

if TYPE_CHECKING:
    from vibesys.run.environment_failure import RunEnvironmentFailure
    from vs_slurm.api import WorkspaceRootProbeReport

__all__ = ["CoreWorkspaceRootProbeEvents"]


class CoreWorkspaceRootProbeEvents:
    """Publish the probe's reports on one run's semantic event stream; frontends word them.

    ``report`` is the run environment's ``workspace_root_probe`` sink. The probe already
    bounds how often it reports a queued job, so every report becomes one event. A probe
    that failed for good leaves the run no Slurm work it could do, so it also ends the
    run through ``environment``, as a failure that names the probe.
    """

    def __init__(self, events: CoreEventWriter, environment: RunEnvironmentFailure) -> None:
        """Route probe reports into ``events``, and a lasting failure into ``environment``."""
        self._events = events
        self._environment = environment

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
            case WorkspaceRootProbeRetrying():
                status = EventStatus.ACTIVE
                data = WorkspaceRootProbeData(
                    state="retrying",
                    failure=report.failure,
                    attempt=report.attempt,
                    retry_in_seconds=report.retry_in_seconds,
                )
            case WorkspaceRootProbeProven():
                status, data = EventStatus.COMPLETED, WorkspaceRootProbeData(state="proven")
            case WorkspaceRootProbeFailed():
                status = EventStatus.FAILED
                data = WorkspaceRootProbeData(state="failed", failure=report.failure)
        if isinstance(report, WorkspaceRootProbeFailed):
            # The run ends first: publishing the event may raise (a broken subscriber), and
            # the probe drops a report that raises, which must not leave the run going.
            self._environment.fail(report.failure)
        self._events.emit(CoreEventType.WORKSPACE_ROOT_PROBE, status=status, data=data)
