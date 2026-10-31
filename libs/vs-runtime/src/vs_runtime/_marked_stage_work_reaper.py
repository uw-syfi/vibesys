"""The stage work of a dead process, found by the mark its commands carry.

A local evaluation opens its candidate under its handle (``create_measured_candidate``'s
``step_tag``), so every stage command carries ``STEP_TAG_ENV=<handle>`` in its environment and
its descendants inherit it. Whatever process still carries a handle of the tag, in any life, is
work the dead process left; a process that left the mark by clearing its environment is a named
residual (``docs/contributing/run-recovery.md``).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vs_runtime._evaluation_jobs import is_handle_of
from vs_runtime._step_runner import ClearUnavailableError
from vs_sandbox.api import STEP_TAG_ENV
from vs_sim.api import ProcessTableUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import RequestId
    from vs_sim.api import MarkedProcess, ProcessTable

_BACKEND = "the process table"
_MAX_SWEEPS = 8
"""Scans before reaping gives up: each one ends every marked process it finds, so more than a
few means the work forks faster than it is ended."""


def _handle_of(tag: RequestId) -> Callable[[str], bool]:
    return partial(is_handle_of, tag)


def marked_with_the_tag(tag: RequestId) -> Callable[[str], bool]:
    """Accepts the mark an agent turn's processes carry: the step's tag itself."""
    return tag.root.__eq__


class MarkedStageWorkReaper:
    """A ``StageWorkReaper`` over the host's process table."""

    def __init__(
        self,
        processes: ProcessTable,
        marked_as: Callable[[RequestId], Callable[[str], bool]] = _handle_of,
    ) -> None:
        """Find and end marked processes through *processes*.

        *marked_as* says which mark values belong to a tag. A local evaluation's stage commands
        carry a handle of the tag (the default); an agent turn's processes carry the tag
        (``marked_with_the_tag``).
        """
        self._processes = processes
        self._marked_as = marked_as

    async def reap(self, tag: RequestId) -> None:
        """End every process carrying a handle of *tag*, scanning until a scan finds none."""
        for _ in range(_MAX_SWEEPS):
            found = self._marked(tag)
            if not found:
                return
            for process in found:
                self._processes.kill(process, STEP_TAG_ENV)
        raise ClearUnavailableError(tag, "stage processes that keep restarting")

    def _marked(self, tag: RequestId) -> tuple[MarkedProcess, ...]:
        try:
            return self._processes.marked(STEP_TAG_ENV, self._marked_as(tag))
        except ProcessTableUnavailableError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
