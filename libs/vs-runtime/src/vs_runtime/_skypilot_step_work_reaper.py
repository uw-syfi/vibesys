"""One step's work in a SkyPilot run: its agent processes, then the remote jobs they asked for.

The agent of a SkyPilot run is a local container; the GPU work it asks for runs as a job on the
run's cluster, requested through the evaluator bridge and journaled with the step's tag. Clearing
the step ends the agent's processes first, so nothing can ask for another job, then cancels the
jobs the journal names for the tag. The cluster stays: it is named per run and other steps' jobs
run on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._stage_marks import agent_mark
from vs_runtime._step_runner import ClearUnavailableError
from vs_sandbox.api.skypilot import SkyPilotCLIError, cancel_step_jobs

if TYPE_CHECKING:
    from vs_core.api import RequestId
    from vs_runtime._local_job_step_runner import StageWorkReaper
    from vs_sandbox.api.skypilot import InvocationJournal, SkyPilotJobRunner
    from vs_sim.api import BlockingRunner

_BACKEND = "the SkyPilot control plane"


class SkyPilotStepWorkReaper:
    """A ``StageWorkReaper`` for a step's agent and its SkyPilot jobs."""

    def __init__(
        self,
        run_id: str,
        agents: StageWorkReaper,
        journal: InvocationJournal,
        runner: SkyPilotJobRunner,
        blocking: BlockingRunner,
    ) -> None:
        """End the agent processes of run *run_id* through *agents*, then cancel jobs of *journal*.

        The jobs are found in *journal* by *runner*. *blocking* carries the journal reads and the
        ``sky`` commands off the event loop.
        """
        self._run_id = run_id
        self._agents = agents
        self._journal = journal
        self._runner = runner
        self._blocking = blocking

    async def reap(self, tag: RequestId) -> None:
        """End the step's agent processes and cancel its remote jobs.

        Raises ``ClearUnavailableError`` when the containers or the cluster's control plane
        cannot be reached: no job is then proven ended.
        """
        await self._agents.reap(tag)
        try:
            await self._blocking.run(
                cancel_step_jobs, self._journal, self._runner, agent_mark(self._run_id, tag)
            )
        except SkyPilotCLIError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
