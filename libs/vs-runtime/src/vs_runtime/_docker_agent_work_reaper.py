"""The agent processes of one step in a run's Docker containers, ended by the step's mark.

The Docker, SkyPilot and Modal environments run the agent in a container of their own, labelled
with the run's id, and every process of a turn carries the turn's tag (``vs_sandbox.step_marks``).
The run-wide reap (``vs_agent.reap_orphaned_agents``) would end the agents of every step; this one
ends the processes of one step and leaves the containers and every other step alone.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vs_agent.api import OrphanReapError, reap_step_agents
from vs_runtime._stage_marks import agent_mark
from vs_runtime._step_runner import ClearUnavailableError
from vs_sim.api import ShieldedBlockingRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import RequestId
    from vs_sandbox.api import DockerCli
    from vs_sim.api import BlockingRunner

_BACKEND = "the agent containers"


class DockerAgentWorkReaper:
    """A ``StageWorkReaper`` over the containers of one run."""

    def __init__(
        self,
        run_id: str,
        docker: DockerCli,
        blocking: BlockingRunner,
        log: Callable[[str], None],
        reap_tagged: Callable[[str, str], int] | None = None,
    ) -> None:
        """Sweep the containers labelled *run_id* through *docker*; *blocking* carries each sweep.

        *reap_tagged* replaces the default ``docker exec`` sweep of one container, for a test.
        A cancelled ``reap`` returns only once the sweep has ended: it is shielded.
        """
        self._run_id = run_id
        self._sweep = partial(
            reap_step_agents, run_id, docker=docker, log=log, reap_tagged=reap_tagged
        )
        self._blocking = ShieldedBlockingRunner(blocking)

    async def reap(self, tag: RequestId) -> None:
        """End every process carrying *tag* in the run's containers.

        Raises ``ClearUnavailableError`` when a container cannot be searched: the daemon or
        the container did not answer, so no process is proven gone.
        """
        try:
            await self._blocking.run(self._sweep, agent_mark(self._run_id, tag))
        except OrphanReapError as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
