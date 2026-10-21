"""Park and relaunch through the real loop, persisted shell and Fake cluster."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_strategy import (
    IMPLEMENTATION,
    IMPLEMENTER,
    Implementation,
    SkeletonStrategy,
)
from tests.support.skeleton_strategy import (
    measurement as measurement_plan,
)
from tests.support.skeleton_world import CandidateResolver, CandidateWriter, open_skeleton_world

from vs_agent.api import AgentClient, AgentTurnRequest
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider
from vs_core.api import (
    AgentMeasurementRequested,
    CancelOwnedJob,
    ObservationStatus,
    ObserveOwnedJob,
    RequestId,
    RunStatus,
    Scope,
    SessionPhase,
    SubmitMeasurement,
    Within,
)
from vs_project.api import RequestState
from vs_project.api.testing import FakeControlInbox
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import (
    CoreRunHost,
    MeasurementRequests,
    OperatorInbox,
    ReceiptStore,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
    report_turns,
    start_core,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionFinished,
    ChannelRunControl,
    ParkRequest,
    RuntimeRunControlChannel,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionLifecycleSink,
    FakeRunControlEventSink,
    ProbedRunClock,
)
from vs_sim.api import yield_now
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimThreads, clock_from, loop_threads
from vs_slurm.api import SlurmJobStatus

from .test_evaluation_requests import (
    ScenarioCluster,
    close_request,
    context_for,
    query,
    resource_of,
    run,
    settled,
    submit,
    world,
)

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
        self.act: Callable[[], bool | None] = lambda: None

    async def sleep(self, seconds: float) -> None:
        if self.act():
            await yield_now()
            return
        await super().sleep(seconds)


async def test_pause_drain_cancels_the_queued_job_exits_and_relaunch_reruns_it(
    tmp_path: Path,
) -> None:
    clock = _Operator(clock_from(1.0))
    threads = SimThreads()
    sink = FakeRunControlEventSink()
    channel = RuntimeRunControlChannel(sink, threads)
    operator = ChannelRunControl(channel, threads)
    config = RunLoopConfig(host_id="host-a", lease_duration=300, control_poll_interval=1)
    with open_skeleton_world(tmp_path, SkeletonStrategy.baseline_only()) as world:
        world.cluster.states = (SlurmJobStatus.PENDING,)
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


@pytest.mark.parametrize("close_scope", [False, True])
async def test_a_closed_scope_or_cancelled_job_cannot_relaunch_after_park(
    *,
    close_scope: bool,
) -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    async with world(cluster) as current:
        resource = resource_of(await submit(current))
        await settled(current, resource)
        park = query(CancelOwnedJob, "park", resource).model_copy(update={"for_park": True})
        assert (await run(current, park)).observation.observation.released
        closing = close_request() if close_scope else query(CancelOwnedJob, "stop-job", resource)
        await run(current, closing)
        before = len(cluster.submissions)
        late = await run(current, query(ObserveOwnedJob, "late-poll", resource), epoch=2)
        assert late.observation.observation.status is ObservationStatus.REJECTED
        assert len(cluster.submissions) == before


class _EvaluatingTurn:
    def __init__(self, channel: RuntimeRunControlChannel) -> None:
        self.threads = loop_threads()
        self.channel = channel
        self.entered = self.threads.event()
        self.evaluated = self.threads.event()
        self.lifecycle = FakeAgentExecutionLifecycleSink()

    def agents(self, root: Path) -> SessionHost:
        writer = CandidateWriter(root)

        def on_turn(request: AgentTurnRequest) -> None:
            self.entered.set()
            assert self.evaluated.wait(HANG_GUARD_S)
            writer(request)

        client = report_turns(
            AgentClient(
                FakeProvider(answer=writer.answer, on_turn=on_turn, threads=self.threads),
                clock=ManualClock(),
                threads=self.threads,
            ),
            self.lifecycle,
            control=self.channel,
        )
        resolver = CandidateResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=writer,
            control=self.channel,
        )
        return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())


async def test_pause_drain_finishes_a_turns_evaluation_requested_after_applied_then_exits(
    tmp_path: Path,
) -> None:
    clock = _Operator(clock_from(1.0))
    threads = loop_threads()
    channel = RuntimeRunControlChannel(FakeRunControlEventSink(), threads)
    inbox = FakeControlInbox()
    turn = _EvaluatingTurn(channel)

    config = RunLoopConfig(
        host_id="host-a", lease_duration=300, control_poll_interval=1, max_concurrent=4
    )
    with open_skeleton_world(tmp_path, agents=turn.agents) as world:
        world.control = channel
        process = world.runtime()
        posted = False
        requested = False
        applied_before_call = False

        def progress() -> bool:
            nonlocal posted, requested, applied_before_call
            changed = False
            core = process.shell.record.envelope.core
            if turn.entered.is_set() and not posted:
                inbox.post(
                    "pause-drain",
                    b'{"action":"park","control_id":"pause-drain"}',
                    ended=lambda: None,
                )
                posted = True
                changed = True
            status = inbox.status("pause-drain", ended=None)
            if status is not None and status.state is RequestState.APPLIED and not requested:
                active = next(
                    inv for inv in core.sessions.invocations if inv.phase is SessionPhase.EXECUTING
                )
                plan = measurement_plan(
                    core.run.facts.baseline, "official", facts=core.run.facts, now_at=clock.now()
                )
                process.shell.admit(
                    AgentMeasurementRequested(
                        scope=active.scope,
                        plan=plan,
                        within=Within(limit=100),
                        call_id="after-applied",
                    ),
                    now_at=clock.now(),
                )
                requested = True
                applied_before_call = True
                changed = True
            if (
                requested
                and not turn.evaluated.is_set()
                and any(
                    job.terminal and job.scope.owner.kind == "attempt"
                    for job in core.evaluation.jobs
                )
            ):
                turn.evaluated.set()
                changed = True
            return changed

        clock.act = progress
        controls = RunControlBridge(
            channel, Steers(), inbox=OperatorInbox(inbox, ChannelRunControl(channel, threads))
        )
        host = CoreRunHost(process.shell, process.delivery, clock, controls)
        start_core(host, config)
        outcome = await drive_core(host, config)
        assert outcome.status is RunStatus.PARKED
        assert applied_before_call
        assert turn.evaluated.is_set()
        evaluation = next(
            job
            for job in process.shell.record.envelope.core.evaluation.jobs
            if job.scope.owner.kind == "attempt"
        )
        assert evaluation.status is ObservationStatus.SUCCEEDED
        assert evaluation.evidence
        assert len(turn.lifecycle.events) == 2
        finished = turn.lifecycle.events[-1]
        assert isinstance(finished, AgentExecutionFinished)
        assert finished.status.value == "completed", finished
        assert not process.shell.in_flight
        assert not world.cluster.cancelled
        process.shell.release_lease(now_at=clock.now())


async def test_pause_drain_waits_for_an_executing_evaluation_to_finish(tmp_path: Path) -> None:
    clock = _Operator(clock_from(1.0))
    channel = RuntimeRunControlChannel(FakeRunControlEventSink(), loop_threads())
    inbox = FakeControlInbox()
    requested_at: float | None = None
    with open_skeleton_world(tmp_path, SkeletonStrategy.baseline_only()) as world:
        world.control = channel
        world.cluster.states = (SlurmJobStatus.RUNNING,)
        process = world.runtime()

        def progress() -> bool:
            nonlocal requested_at
            if world.cluster.submissions and requested_at is None:
                inbox.post(
                    "pause-drain",
                    b'{"action":"park","control_id":"pause-drain"}',
                    ended=lambda: None,
                )
                requested_at = clock.now()
                return True
            if requested_at is not None and clock.now() >= requested_at + 120:
                world.cluster.finish_jobs()
            return False

        clock.act = progress
        controls = RunControlBridge(
            channel,
            Steers(),
            inbox=OperatorInbox(inbox, ChannelRunControl(channel, loop_threads())),
        )
        host = CoreRunHost(process.shell, process.delivery, clock, controls)
        config = RunLoopConfig(host_id="host-a", lease_duration=300, max_concurrent=4)
        start_core(host, config)
        outcome = await drive_core(host, config)
        assert outcome.status is RunStatus.PARKED
        job = process.shell.record.envelope.core.evaluation.jobs[0]
        assert job.status is ObservationStatus.SUCCEEDED
        assert not world.cluster.cancelled
        process.shell.release_lease(now_at=clock.now())
