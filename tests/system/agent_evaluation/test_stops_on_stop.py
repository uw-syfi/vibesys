"""An operator stop ends the evaluations agents submitted, through the production loop.

On the legacy path the host cancelled the evaluation service's outstanding jobs when a stop
arrived. Core runs have no such service: an agent's submission is a core measurement, so the
stop is settled by core and the shell. This test stops a run while an agent-submitted
evaluation runs on the Fake cluster and checks that the job does not outlive the run.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from tests.support.skeleton_world import LEASE, Process
from tests.system.agent_evaluation.test_bridge import Scenario, scenario

from vs_core.api import StopMode
from vs_evaluation.api import SocketFailure, SocketSuccess, SubmitCall
from vs_runtime.api.core import CoreRunHost, RunControlBridge, RunLoopConfig, drive_core
from vs_runtime.api.infrastructure import RunStopped, RuntimeRunControlChannel
from vs_runtime.api.testing import ProbedRunClock
from vs_sim.api.testing import VirtualClock, clock_from, loop_threads
from vs_slurm.api import SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_runtime.api.core import RunOutcome


SOCKET_REPLY = TypeAdapter(SocketSuccess | SocketFailure)
GRACE_S = 60.0
"""The host's grace period (``STOP_GRACE_S``): the time a stop may take before the host cancels the loop."""


def _ignore(transition: object) -> None:
    del transition


class _NoSteers:
    def put(self, ref: object, text: str) -> None:
        del ref, text


class _OnSleep(ProbedRunClock):
    """A run clock that runs ``act`` after each sleep of the loop."""

    def __init__(self, inner: VirtualClock, act: Callable[[], None]) -> None:
        super().__init__(inner)
        self._act = act

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        self._act()


async def _until_the_host_gives_up(
    running: asyncio.Task[RunOutcome], clock: ProbedRunClock, stopped_at: list[float]
) -> None:
    """Wait for the loop; ``GRACE_S`` after the stop, cancel it as the host does.

    The attempt drains, and its agent's job never ends, so the loop would wait for the
    continuation's deadline. The host's grace bound is what ends the run.
    """
    while not running.done():
        if stopped_at and clock.now() >= stopped_at[0] + GRACE_S:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            raise RunStopped
        await clock.sleep(1.0)
    await running


async def _stopped_run(
    played: Scenario,
    clock: ProbedRunClock,
    channel: RuntimeRunControlChannel,
    stopped_at: list[float],
) -> tuple[Process, bool]:
    process = played.world.runtime()
    (bridge,) = played.bridge
    controls = RunControlBridge(
        channel,
        _NoSteers(),  # type: ignore[arg-type]  # only stores steer text; none is sent
    )
    host = CoreRunHost(process.shell, process.delivery, clock, controls)
    process.shell.start("skeleton", now_at=1.0, lease_duration=LEASE)
    bridge.attach(process.shell, clock)
    await bridge.serve()
    stopped = False
    running = asyncio.ensure_future(
        drive_core(
            host,
            RunLoopConfig(
                host_id="loop",
                lease_duration=LEASE,
                control_poll_interval=1.0,
                min_sleep=0.05,
                max_dispatches=5000,
            ),
        )
    )
    try:
        await _until_the_host_gives_up(running, clock, stopped_at)
    except RunStopped:
        stopped = True
    finally:
        await bridge.close()
    return process, stopped


async def test_a_stop_while_an_agent_evaluation_runs_starts_no_further_work(tmp_path: Path) -> None:
    channel = RuntimeRunControlChannel(_ignore, threads=loop_threads())
    holder: list[Scenario] = []
    at_stop: list[int] = []
    stopped_at: list[float] = []

    def stop() -> None:
        if not at_stop:
            at_stop.append(len(holder[0].world.cluster.submissions))
            stopped_at.append(clock.now())
            channel.request_stop(StopMode.DRAIN)

    def act() -> None:
        cluster = holder[0].world.cluster
        if cluster.submissions:
            # The baseline measurement was scripted to end at once; the agent's job never does.
            cluster.states = (SlurmJobStatus.RUNNING,) * 100_000
        if len(cluster.submissions) >= 2:
            stop()

    clock = _OnSleep(clock_from(1.0), act)
    async with scenario(tmp_path) as played:
        holder.append(played)
        process, stopped = await _stopped_run(played, clock, channel, stopped_at)
        submitted_at_stop = at_stop[0]
        core = process.shell.record.envelope.core
        cluster = played.world.cluster
        assert stopped
        # The search is still draining the attempt whose agent waits on a job that never ends:
        # the stop is recorded, the run is not over, and the host's grace bound ends the loop.
        assert core.run.result is None
        assert [c.action for c in core.run.controls] == ["stop"]
        assert len(cluster.submissions) == submitted_at_stop, "a stop starts no further work"
        token = played.writer.descriptor_env["VS_EVALUATION_TOKEN"]
        frame = SubmitCall(token=token).model_dump_json().encode()
        reply = SOCKET_REPLY.validate_json((await played.bridge[0].answer(frame))[0])
        assert isinstance(reply, SocketFailure), "a stopped run admits no new agent submission"
        assert len(cluster.submissions) == submitted_at_stop
        # The host closes the evaluation executor when the run exits, which cancels every job
        # still on the cluster, the agent's included.
        assert cluster.cancelled == []
        for evaluation in played.world.evaluations:
            await evaluation.close()
        assert sorted(cluster.cancelled) == sorted(cluster.submissions[1:])
