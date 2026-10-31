"""The stage processes of a dead process, found by the mark their commands carry.

A local evaluation opens its candidate under its handle (``create_measured_candidate``'s
``step_tag``), so every stage command carries ``STEP_TAG_ENV=<handle>``: in its environment,
which its descendants inherit, and as an argument of its process group leader. What the host
shows of that mark is the process table's (``vs_sim.process_table``); the reaper ends whatever
the table shows carrying a handle of the tag, in any life. Work the host cannot show (a
``setsid`` child on macOS, or the members of a group whose leader died first) is a named
residual (``docs/contributing/run-recovery.md``).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vs_runtime._stage_marks import is_stage_mark_of
from vs_runtime._step_runner import ClearUnavailableError
from vs_sandbox.api import STEP_TAG_ENV
from vs_sim.api import ProcessSurvivedError, ProcessTableUnavailableError

if TYPE_CHECKING:
    from vs_core.api import RequestId
    from vs_sim.api import MarkedProcess, ProcessTable

_BACKEND = "the process table"
_MAX_SWEEPS = 8
"""Scans before reaping gives up: each one ends every marked process it finds, so more than a
few means the work forks faster than it is ended."""


class ProcessStageWorkReaper:
    """A ``StageWorkReaper`` over the host's process table."""

    def __init__(self, processes: ProcessTable, run_id: str) -> None:
        """Find and end the marked processes of run *run_id* through *processes*."""
        self._processes = processes
        self._run_id = run_id

    async def reap(self, tag: RequestId) -> None:
        """End every process carrying a handle of *tag*, scanning until a scan finds none."""
        for _ in range(_MAX_SWEEPS):
            found = self._marked(tag)
            if not found:
                return
            for process in found:
                self._kill(tag, process)
        raise ClearUnavailableError(tag, "stage processes that keep restarting")

    def _kill(self, tag: RequestId, process: MarkedProcess) -> None:
        try:
            self._processes.kill(process, STEP_TAG_ENV)
        except ProcessSurvivedError as error:
            raise ClearUnavailableError(tag, "stage processes that survive SIGKILL") from error

    def _marked(self, tag: RequestId) -> tuple[MarkedProcess, ...]:
        try:
            return self._processes.marked(
                STEP_TAG_ENV, partial(is_stage_mark_of, self._run_id, tag)
            )
        except ProcessTableUnavailableError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
