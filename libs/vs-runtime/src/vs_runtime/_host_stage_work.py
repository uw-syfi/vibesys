"""What a run's host can show of a dead process's stage work, and the reaper built over it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_runtime._container_stage_work_reaper import ContainerStageWorkReaper
from vs_runtime._each_stage_work_reaper import EachStageWorkReaper
from vs_runtime._process_stage_work_reaper import ProcessStageWorkReaper

if TYPE_CHECKING:
    from vs_runtime._local_job_step_runner import StageWorkReaper
    from vs_sandbox.api import StepContainers
    from vs_sim.api import ProcessTable


@dataclass(frozen=True)
class HostStageWork:
    """The process table and, in a Docker run, the step containers of the host of run ``run_id``."""

    processes: ProcessTable
    containers: StepContainers | None
    """``None`` for a run environment without Docker."""
    run_id: str

    def reaper(self) -> StageWorkReaper:
        """A reaper that ends the processes and, where there are any, the containers of a tag."""
        reapers: list[StageWorkReaper] = [ProcessStageWorkReaper(self.processes, self.run_id)]
        if self.containers is not None:
            reapers.append(ContainerStageWorkReaper(self.containers, self.run_id))
        return EachStageWorkReaper(*reapers)
