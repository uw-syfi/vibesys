"""The Docker candidate containers of one step, removed by the step label they were created with."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._stage_marks import is_stage_mark_of
from vs_runtime._step_runner import ClearUnavailableError
from vs_sandbox.api import StepContainerError

if TYPE_CHECKING:
    from vs_core.api import RequestId
    from vs_sandbox.api import StepContainer, StepContainers

_BACKEND = "the container runtime"


class ContainerStageWorkReaper:
    """A ``StageWorkReaper`` over the step containers of one run.

    Only the containers labelled with a handle of the tag are removed, so other steps' work in
    the same live run is untouched. It returns after a second listing shows none left.
    """

    def __init__(self, containers: StepContainers, run_id: str) -> None:
        """Find and remove the containers of run *run_id* through *containers*."""
        self._containers = containers
        self._run_id = run_id

    async def reap(self, tag: RequestId) -> None:
        """Remove every container of *tag*, then list again to verify none remains."""
        try:
            for container in self._of(tag):
                self._containers.remove(container)
            left = self._of(tag)
        except StepContainerError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
        if left:
            raise ClearUnavailableError(tag, "containers that outlive their removal")

    def _of(self, tag: RequestId) -> tuple[StepContainer, ...]:
        return tuple(
            c for c in self._containers.listed() if is_stage_mark_of(self._run_id, tag, c.tag)
        )
