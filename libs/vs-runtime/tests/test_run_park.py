"""Park and relaunch through the real loop, persisted shell and Fake cluster."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import open_skeleton_world

from vs_core.api import ObservationStatus, RequestId, RunStatus, Scope, SubmitMeasurement
from vs_runtime.api.core import (
    CoreRunHost,
    MeasurementRequests,
    ReceiptStore,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import ChannelRunControl, ParkRequest, RuntimeRunControlChannel
from vs_runtime.api.testing import FakeRunControlEventSink, ProbedRunClock
from vs_sim.api.testing import SimThreads, clock_from
from vs_slurm.api import SlurmJobStatus

from .test_evaluation_requests import context_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_core.api import ArtifactRef
    from vs_sim.api.testing import VirtualClock


class Steers:
    def put(self, ref: ArtifactRef, text: str) -> None:
        del ref, text


class _Operator(ProbedRunClock):
    def __init__(self, clock: VirtualClock) -> None:
        super().__init__(clock)
        self.act: Callable[[], None] = lambda: None

    async def sleep(self, seconds: float) -> None:
        self.act()
        await super().sleep(seconds)


async def test_park_cancels_the_live_job_exits_and_resume_reruns_it(tmp_path: Path) -> None:
    clock = _Operator(clock_from(1.0))
    threads = SimThreads()
    sink = FakeRunControlEventSink()
    channel = RuntimeRunControlChannel(sink, threads)
    operator = ChannelRunControl(channel, threads)
    config = RunLoopConfig(host_id="host-a", lease_duration=300, control_poll_interval=1)
    with open_skeleton_world(tmp_path, SkeletonStrategy.baseline_only()) as world:
        world.cluster.states = (SlurmJobStatus.RUNNING,) * 1000
        process = world.runtime()

        def request_park() -> None:
            if world.cluster.submissions:
                operator.submit(ParkRequest(control_id="park"))
                clock.act = lambda: None

        clock.act = request_park
        controls = RunControlBridge(channel, Steers())
        host = CoreRunHost(process.shell, process.delivery, clock, controls)
        start_core(host, config)
        outcome = await drive_core(host, config)
        assert outcome.status == RunStatus.PARKED, (sink.transitions, outcome)
        assert outcome.result is None
        assert not process.shell.in_flight
        saved = process.shell.record.envelope.core
        assert saved.evaluation.jobs
        assert not saved.evaluation.jobs[0].terminal
        assert world.cluster.cancelled
        before = len(world.cluster.submissions)
        process.shell.release_lease(now_at=clock.now())

        world.cluster.states = (SlurmJobStatus.COMPLETED,)
        resumed = world.runtime()
        host = CoreRunHost(resumed.shell, resumed.delivery, clock)
        start_core(host, config)
        finished = await drive_core(host, config)
        assert finished.status == RunStatus.TERMINAL
        assert len(world.cluster.submissions) > before
        assert resumed.shell.record.envelope.core.evaluation.jobs[0].terminal
        source = saved.evaluation.jobs[0]
        reuse = SubmitMeasurement(
            request_id=RequestId(root="reuse-after-park"),
            scope=Scope(owner=source.scope.owner, generation=source.scope.generation + 1),
            plan=source.plan,
            reuses=source.submission_id,
        )
        adapter = MeasurementRequests(
            world.evaluations[-1], ReceiptStore(world.env.receipts_namespace(), threads=threads)
        )
        context = context_for(reuse, epoch=resumed.shell.record.envelope.fence.epoch).model_copy(
            update={"fence": resumed.shell.record.envelope.fence}
        )
        submitted = len(world.cluster.submissions)
        reused = await adapter.execute(reuse, context)
        assert reused.observation.observation.status is ObservationStatus.SUCCEEDED
        assert len(world.cluster.submissions) == submitted
        resumed.shell.release_lease(now_at=clock.now())
