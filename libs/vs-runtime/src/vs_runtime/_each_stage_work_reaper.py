"""Several reapers acting as one: a host holds processes and, in a Docker run, containers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._step_runner import ClearUnavailableError

if TYPE_CHECKING:
    from vs_core.api import RequestId
    from vs_runtime._local_job_step_runner import StageWorkReaper


class EachStageWorkReaper:
    """A ``StageWorkReaper`` that asks each of its reapers in order.

    Every reaper runs even if an earlier one raises, so one unreachable backend does not leave
    the work of another standing; the first error is raised once all have run.
    """

    def __init__(self, *reapers: StageWorkReaper) -> None:
        """Ask *reapers* in the order given."""
        self._reapers = reapers

    async def reap(self, tag: RequestId) -> None:
        """Reap *tag* with each reaper; raise the first error after all have run."""
        first: ClearUnavailableError | None = None
        for reaper in self._reapers:
            try:
                await reaper.reap(tag)
            except ClearUnavailableError as error:
                first = first or error
        if first is not None:
            raise first
