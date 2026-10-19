"""A run's workspace-root probe, run beside the run, and the gate its Slurm work passes.

The probe (``workspace_probe``) proves that a local Slurm root is shared both ways
with the compute nodes. Nothing a run does before its first Slurm job depends on
that: agents plan and edit in the run's own workspace. So the run starts the probe
here, on a thread of its own, and goes on; only the work that does depend on the
root, a Slurm submission, waits for the proof through :meth:`WorkspaceRootProof.check`
or :meth:`WorkspaceRootProof.require`, and a failed proof refuses that work with the
probe's own error. The probe keeps every durable guarantee it had when it ran
inline: exactly one job per attempt, restart reconciliation, cleanup on every path.

A probe the queue never scheduled says nothing about the root, so the proof tries it
again after an exponential backoff until ``workspace_root_probe_deadline_seconds``
passes; Slurm work keeps waiting meanwhile. Any other failure is lasting and settles
the proof at once.
"""

from __future__ import annotations

import logging
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
# The wait before the first retry of a probe the queue never scheduled; each later wait
# doubles, up to the cap, and never runs past the probe's deadline.
_FIRST_RETRY_SECONDS = 60.0
_RETRY_CAP_SECONDS = 1800.0

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceRootProbeProven:
    """The probe proved the root shared both ways; Slurm work may go ahead."""


@dataclass(frozen=True, slots=True)
class WorkspaceRootProbeFailed:
    """The probe could not prove the root; ``failure`` is its error, as Slurm work sees it."""

    failure: str


@dataclass(frozen=True, slots=True)
class WorkspaceRootProbeRetrying:
    """An attempt ended without saying anything about the root; the proof tries again.

    ``failure`` is that attempt's error, ``attempt`` the number of the attempt that will
    follow (the second is 2), and ``retry_in_seconds`` how long the proof waits first.
    """

    failure: str
    attempt: int
    retry_in_seconds: float


type WorkspaceRootProbeReport = (
    WorkspaceRootProbeQueued
    | WorkspaceRootProbeRetrying
    | WorkspaceRootProbeProven
    | WorkspaceRootProbeFailed
)
"""What a running proof reports: its job waits in the queue, it retries, or how it ended."""


class WorkspaceRootProof:
    """One run's workspace-root probe on its own thread, and the gate after it.

    ``start`` begins the probe and returns at once. ``check`` and ``require`` answer
    whether the root is proven, raising the probe's ``SlurmWorkspaceRootProbeError``
    once it failed for good. ``close`` stops a probe still running: its job is
    cancelled, settled and cleaned, and a resume of the run retries it. ``report``
    hears that the job waits in the queue (bounded, see ``probe_workspace_root``), that
    the proof retries, and how it ended, before any caller can see that ending. A
    report that raises is logged and dropped: telling the run must never decide it.
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
        outcome: WorkspaceRootProbeProven | WorkspaceRootProbeFailed
        try:
            self._prove_within_deadline()
            outcome = WorkspaceRootProbeProven()
        except SlurmWorkspaceRootProbeError as error:
            outcome = WorkspaceRootProbeFailed(failure=str(error))
        except BaseException:
            # A defect: the gate, neither proven nor failed with a reason, fails closed
            # (``check``), and the error propagates on this thread.
            self._settled.set()
            raise
        # The run hears the ending before any Slurm work can see it, so a run that ends
        # on a failed proof ends for that reason, not for what the refusal did after.
        self._tell(outcome)
        if isinstance(outcome, WorkspaceRootProbeProven):
            self._proven = True
        else:
            self._failure = outcome.failure
        self._settled.set()

    def _prove_within_deadline(self) -> None:
        """Probe until proven, a lasting failure, a stop, or the deadline (each raises)."""
        deadline_seconds = self._runner.config.workspace_root_probe_deadline_seconds
        started = self._threads.now()
        delay = _FIRST_RETRY_SECONDS
        attempt = 1
        while True:
            try:
                probe_workspace_root(
                    self._runner,
                    state_root=self._state_root,
                    run_id=self._run_id,
                    pause=self._threads.sleep,
                    watch=WorkspaceRootProbeWatch(stop=self._stop, queued=self._tell),
                )
            except SlurmWorkspaceRootProbeError as error:
                if not error.retryable or self._stop.is_set():
                    raise
                waited = self._threads.now() - started
                remaining = deadline_seconds - waited
                if remaining <= 0:
                    raise SlurmWorkspaceRootProbeError.deadline_passed(
                        error,
                        attempts=attempt,
                        waited_seconds=waited,
                        deadline_seconds=deadline_seconds,
                    ) from error
                wait = min(delay, remaining)
                attempt += 1
                self._tell(
                    WorkspaceRootProbeRetrying(
                        failure=str(error), attempt=attempt, retry_in_seconds=wait
                    )
                )
                if self._stop.wait(wait):
                    raise SlurmWorkspaceRootProbeError.stopped(None) from error
                delay = min(2 * delay, _RETRY_CAP_SECONDS)
            else:
                return

    def _tell(self, report: WorkspaceRootProbeReport) -> None:
        """Report to the run; a sink that fails (a full disk, a broken subscriber) is logged."""
        if self._report is None:
            return
        try:
            self._report(report)
        except Exception:  # noqa: BLE001  # lint-waiver: LW-324301 [BLE001]; the sink is the run's (events, a log file), and any failure of it must leave the probe's outcome intact.
            # > Catching only OSError would let a subscriber's own error fail the probe and
            # > refuse every Slurm job; a sink contract that may not raise cannot be enforced
            # > on callers' code. Logging and dropping one report loses only that notice.
            _LOG.exception("the workspace-root probe could not report %r", report)


__all__ = [
    "WorkspaceRootProbeFailed",
    "WorkspaceRootProbeProven",
    "WorkspaceRootProbeReport",
    "WorkspaceRootProbeRetrying",
    "WorkspaceRootProof",
]
