"""A run's workspace-root probe, run beside the run, and the gate its Slurm work passes.

The probe (``workspace_probe``) proves that a local Slurm root is shared both ways
with the compute nodes. Nothing a run does before its first Slurm job depends on
that: agents plan and edit in the run's own workspace. So the run starts the probe
here, on a thread of its own, and goes on; only the work that does depend on the
root, a Slurm submission, waits for the proof through :meth:`WorkspaceRootProof.check`
or :meth:`WorkspaceRootProof.require`, and a failed proof refuses that work with the
probe's own error. The probe keeps every durable guarantee it had when it ran
inline: exactly one job per attempt, restart reconciliation, cleanup on every path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import SlurmWorkspaceRootProbeError
from .workspace_probe import (
    WorkspaceRootProbeQueued,
    WorkspaceRootProbeWatch,
    probe_workspace_root,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sim.api import Event, Threads, Worker

    from .runner import SlurmJobRunner

# How often a blocked ``require`` looks at its caller's cancel flag. The flag belongs to
# the caller and cannot wake the proof's own event; the proof settling wakes it at once.
_CANCEL_POLL_SECONDS = 0.5
# How long ``close`` waits for a stopped probe to cancel, settle and clean its job. Past
# it the run's exit goes on; the probe's durable record lets a resume finish the job.
_CLOSE_BOUND_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class WorkspaceRootProbeProven:
    """The probe proved the root shared both ways; Slurm work may go ahead."""


@dataclass(frozen=True, slots=True)
class WorkspaceRootProbeFailed:
    """The probe could not prove the root; ``failure`` is its error, as Slurm work sees it."""

    failure: str


type WorkspaceRootProbeReport = (
    WorkspaceRootProbeQueued | WorkspaceRootProbeProven | WorkspaceRootProbeFailed
)
"""What a running proof reports: its job waits in the queue, or how the proof ended."""


class WorkspaceRootProof:
    """One run's workspace-root probe on its own thread, and the gate after it.

    ``start`` begins the probe and returns at once. ``check`` and ``require`` answer
    whether the root is proven, raising the probe's ``SlurmWorkspaceRootProbeError``
    once it failed. ``close`` stops a probe still running: its job is cancelled,
    settled and cleaned, and a resume of the run retries it. ``report`` hears that the
    job waits in the queue (bounded, see ``probe_workspace_root``) and how it ended.
    """

    def __init__(
        self,
        runner: SlurmJobRunner,
        *,
        state_root: Path,
        run_id: str,
        threads: Threads,
        report: Callable[[WorkspaceRootProbeReport], None] | None = None,
    ) -> None:
        """Bind the probe's runner (the run's clock) and durable record; start nothing yet."""
        self._runner = runner
        self._state_root = state_root
        self._run_id = run_id
        self._threads = threads
        self._report = report
        self._settled = threads.event()
        self._stop = threads.event()
        self._proven = False
        self._failure: str | None = None
        self._worker: Worker | None = None

    def start(self) -> None:
        """Start the probe on its own thread; a second call does nothing."""
        if self._worker is None:
            self._worker = self._threads.spawn(self._prove, name="vibesys-slurm-root-probe")

    def check(self) -> bool:
        """Whether the root is proven: False while the probe runs; raise once it failed."""
        if not self._settled.is_set():
            return False
        if self._proven:
            return True
        # A new error per raise: callers on several threads each get their own traceback.
        if self._failure is None:
            raise SlurmWorkspaceRootProbeError.infrastructure()
        raise SlurmWorkspaceRootProbeError(self._failure)

    def require(self, cancel: Event) -> bool:
        """Block until the root is proven (True) or ``cancel`` is set first (False).

        Raises the probe's error once it failed. The wait is bounded by the probe's
        own queue and run limits, and by the caller's ``cancel``.
        """
        while not self.check():
            if cancel.is_set():
                return False
            self._settled.wait(_CANCEL_POLL_SECONDS)
        return True

    def close(self) -> None:
        """Stop a running probe and wait, bounded, for it to clean up its job."""
        self._stop.set()
        if self._worker is not None:
            self._worker.join(_CLOSE_BOUND_SECONDS)

    def _prove(self) -> None:
        outcome: WorkspaceRootProbeReport
        try:
            probe_workspace_root(
                self._runner,
                state_root=self._state_root,
                run_id=self._run_id,
                pause=self._threads.sleep,
                watch=WorkspaceRootProbeWatch(stop=self._stop, queued=self._report),
            )
            self._proven = True
            outcome = WorkspaceRootProbeProven()
        except SlurmWorkspaceRootProbeError as error:
            self._failure = str(error)
            outcome = WorkspaceRootProbeFailed(failure=self._failure)
        finally:
            # Any other error is a defect: it propagates on this thread, and the gate,
            # neither proven nor failed with a reason, fails closed (``check``).
            self._settled.set()
        if self._report is not None:
            self._report(outcome)


__all__ = [
    "WorkspaceRootProbeFailed",
    "WorkspaceRootProbeProven",
    "WorkspaceRootProbeReport",
    "WorkspaceRootProof",
]
