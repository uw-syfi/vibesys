"""One Slurm evaluation contract, run over the Fake cluster and over recorded scheduler traces.

Every case here runs the same assertions against each *world*: the Fake cluster
with a production-like timeline, and the real ``SlurmJobRunner`` and
``SlurmCluster`` driven by a ``TraceConnector`` that replays what a production
cluster showed (``squeue`` and ``sacct`` output over time, and its reaction to
``scancel``). A behavior the Fake lacks but a trace shows fails here.

Time is a ``ManualClock`` that moves when the executor paces itself and by a fixed
latency per remote command, so nothing sleeps.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import vs_evaluation.api.testing as evaluation_testing
from vs_evaluation.api import (
    EvaluationCoordinator,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    PollPhase,
)
from vs_evaluation.api.testing import FakeClock
from vs_sandbox.api.slurm import SlurmEvaluationExecutor, SlurmStagePayload
from vs_slurm.api import (
    CANCEL_REACTIONS,
    LIFETIMES,
    ClusterCancelOutcome,
    ClusterCollectOutcome,
    ClusterInspectOutcome,
    ClusterObservation,
    ClusterSubmitOutcome,
    ClusterTarget,
    FakeCluster,
    ManualClock,
    SchedulerTrace,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmCluster,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmSshTransport,
    TraceConnector,
    TraceStep,
)
from vs_slurm.api import SlurmBatchStageResult as _StageResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_POLL_INTERVAL_S = 10.0
# The executor waits this long for a cancelled job to end before it leaves the
# evaluation CANCELING. It exceeds every recorded teardown.
_CONFIRMATION_S = 120.0
_RANK = {
    EvaluationState.QUEUED: 0,
    EvaluationState.STARTING: 1,
    EvaluationState.RUNNING: 2,
    EvaluationState.CANCELING: 3,
    EvaluationState.SUCCEEDED: 4,
    EvaluationState.FAILED: 4,
    EvaluationState.CANCELED: 4,
}
_PHASE_RANK = {
    PollPhase.UNSUBMITTED: 0,
    PollPhase.QUEUED: 1,
    PollPhase.RUNNING: 2,
    PollPhase.ENDED: 3,
}
_FOREVER = 10**9


class _ObservedCluster:
    """A real cluster behind a wrapper that counts scancel requests and signals acceptance."""

    def __init__(self, inner: FakeCluster | SlurmCluster) -> None:
        self._inner = inner
        self.scancels = 0
        self.accepted = threading.Event()

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        outcome = self._inner.submit(request, operation_id=operation_id)
        self.accepted.set()
        return outcome

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        return self._inner.inspect(target, by_job_id=by_job_id)

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        self.scancels += 1
        return self._inner.cancel(target, by_job_id=by_job_id)

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCollectOutcome:
        return self._inner.collect(target, by_job_id=by_job_id, observed=observed)


class _TimelineFake(FakeCluster):
    """The Fake with one fixed production-like timeline for every batch."""

    def __init__(self, *, queue_wait_s: float, run_s: float, completing_s: float) -> None:
        super().__init__(clock=ManualClock())
        self._timeline = (queue_wait_s, run_s, completing_s)

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if isinstance(request, SlurmBatchRequest):
            queue_wait_s, run_s, completing_s = self._timeline
            self.script(
                operation_id,
                queue_wait_s=queue_wait_s,
                run_s=run_s,
                completing_s=completing_s,
                result=SlurmBatchResult(
                    job_id="0",
                    job_exit_code=0,
                    job_output="",
                    stages=tuple(
                        _StageResult(
                            name=stage.name,
                            exit_code=0,
                            stdout="ok",
                            stderr="",
                            elapsed_seconds=1.0,
                            skipped=False,
                        )
                        for stage in request.stages
                    ),
                    phase_timings_seconds={},
                    content_cache_hits=0,
                ),
            )
        return super().submit(request, operation_id=operation_id)


class _Faults:
    """A connection to the scheduler that drops one chosen command, then recovers."""

    def __init__(self, inner: TraceConnector) -> None:
        self._inner = inner
        self._countdown: int | None = None

    def drop_command(self, index: int) -> None:
        """Fail the ``index``-th command from now (0 is the next one) with a transport error."""
        self._countdown = index

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if self._countdown is not None:
            if self._countdown == 0:
                self._countdown = None
                return subprocess.CompletedProcess(argv, 255, "", "connection lost")
            self._countdown -= 1
        return self._inner(argv, stdin=stdin, timeout=timeout)


@dataclass
class _World:
    """One scheduler behind the executor, with its clock and counters."""

    cluster: _ObservedCluster
    clock: ManualClock
    transport_scancels: Callable[[], int]
    root: Path
    commands: Callable[[], tuple[str, ...]] = lambda: ()
    faults: _Faults | None = None


@dataclass(frozen=True)
class _WorldSpec:
    label: str
    build: Callable[[Path], _World]


def _fake_world(
    root: Path, *, completing_s: float = 35.0, run_s: float = 168.0, queue_wait_s: float = 95.0
) -> _World:
    fake = _TimelineFake(queue_wait_s=queue_wait_s, run_s=run_s, completing_s=completing_s)
    assert isinstance(fake.clock, ManualClock)
    return _World(_ObservedCluster(fake), fake.clock, lambda: 0, root)


def _replay_world(
    root: Path,
    lifetime: SchedulerTrace,
    running: SchedulerTrace,
    pending: SchedulerTrace | None = None,
) -> _World:
    clock = ManualClock()
    remote = root / "remote"
    remote.mkdir()
    connector = TraceConnector(
        root / "connector",
        clock=clock,
        lifetime=lifetime,
        on_cancel_pending=pending or CANCEL_REACTIONS["cancel-pending"],
        on_cancel_running=running,
        stage_weights=(3.0, 1.0),
    )
    faults = _Faults(connector)
    runner = SlurmJobRunner(
        SlurmConfig(
            name="replay",
            remote_workspace_root=str(remote),
            transport=SlurmConnectorTransport(kind="connector", command=("trace-connector",)),
        ),
        process=faults,
        clock=clock.now,
        pause=clock.advance,
    )
    cluster = SlurmCluster(runner, state_root=root / "identity")
    return _World(
        _ObservedCluster(cluster), clock, connector.scancels, root, connector.commands, faults
    )


def _replay(lifetime: str, running: str) -> _WorldSpec:
    return _WorldSpec(
        f"replay:{lifetime}+{running}",
        lambda root: _replay_world(root, LIFETIMES[lifetime], CANCEL_REACTIONS[running]),
    )


WORLDS = (
    _WorldSpec("fake", _fake_world),
    _replay("normal-run", "cancel-running"),
    _replay("normal-run", "cancel-running-slow-teardown"),
    _replay("pending-then-running", "cancel-running"),
    _replay("pending-then-running", "cancel-running-slow-teardown"),
    _replay("job-ends-first", "cancel-running-slow-teardown"),
)
_worlds = pytest.mark.parametrize("spec", WORLDS, ids=lambda spec: spec.label)


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="contract",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="contract"),
        poll_interval_seconds=_POLL_INTERVAL_S,
    )


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        key="contract",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(command="true", timeout_seconds=5).model_dump(
                    mode="json"
                ),
            )
            for name in ("accuracy", "benchmark")
        ),
    )


def _executor(
    world: _World, pause: Callable[[float], None] | None = None
) -> SlurmEvaluationExecutor:
    """An executor over the world; by default its waiting advances the world's clock."""
    workspace = world.root / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.txt").write_text("x", encoding="utf-8")
    return SlurmEvaluationExecutor(
        _config(),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=world.root / "handles",
        cluster=world.cluster,
        pause=pause or world.clock.advance,
        cancel_confirmation_seconds=_CONFIRMATION_S,
    )


def _stack(
    world: _World, pause: Callable[[float], None] | None = None
) -> tuple[SlurmEvaluationExecutor, EvaluationCoordinator]:
    executor = _executor(world, pause)
    store = evaluation_testing.InMemoryEvaluationStore()
    return executor, EvaluationCoordinator(executor, store, FakeClock())


_OPS = st.lists(st.sampled_from(("snapshot", "inspect_only", "poll", "wait")), max_size=30)
_PROPERTY = settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])


async def _publishes_monotonically(spec: _WorldSpec, operations: list[str]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        executor, coordinator = _stack(spec.build(Path(raw)))
        handle = await coordinator.submit(_request())
        states: list[int] = []
        phases: list[tuple[int, int]] = []

        async def step(operation: str) -> EvaluationState:
            match operation:
                case "snapshot":
                    record = await coordinator.snapshot(handle.id)
                    states.append(_RANK[record.state])
                    return record.state
                case "inspect_only":
                    inspected = await coordinator.inspect_snapshot(handle.id)
                    if inspected is not None:
                        states.append(_RANK[inspected.state])
                case "poll":
                    polled = await executor.poll(handle.id)
                    if polled.phase in _PHASE_RANK:
                        phases.append((polled.attempt, _PHASE_RANK[polled.phase]))
                case _:
                    await asyncio.sleep(0)
            return await coordinator.recorded_status(handle.id)

        for operation in operations:
            await step(operation)
        for _ in range(10_000):
            if await step("snapshot") is EvaluationState.SUCCEEDED:
                break
            await executor.wait_for_change(handle.id, 60.0)
        assert states[-1] == _RANK[EvaluationState.SUCCEEDED]
        assert states == sorted(states)
        assert phases == sorted(phases)
        await executor.close()


@_worlds
@_PROPERTY
@given(operations=_OPS)
def test_published_lifecycle_never_goes_backwards(spec: _WorldSpec, operations: list[str]) -> None:
    """Queueing, requeue and teardown readings in any interleaving never regress or error."""
    asyncio.run(_publishes_monotonically(spec, operations))


async def _stopped_after(spec: _WorldSpec, delay_s: float) -> None:
    with tempfile.TemporaryDirectory() as raw:
        world = spec.build(Path(raw))
        executor, coordinator = _stack(world)
        handle = await coordinator.submit(_request())
        await asyncio.to_thread(world.cluster.accepted.wait)
        world.clock.advance(delay_s)
        record = await coordinator.cancel(handle.id)
        # A job that ended before the stop is a success; every other stop is CANCELED.
        assert record.state in {EvaluationState.CANCELED, EvaluationState.SUCCEEDED}
        assert world.cluster.scancels <= 1
        assert world.transport_scancels() <= 1
        if record.state is EvaluationState.CANCELED:
            assert world.cluster.scancels == 1
        await executor.close()
        assert world.cluster.scancels <= 1


@_worlds
@_PROPERTY
@example(delay_s=3.0)
@example(delay_s=50.0)
@example(delay_s=83.0)
@example(delay_s=150.0)
@given(delay_s=st.floats(0, 400, allow_nan=False))
def test_a_stop_of_an_active_job_ends_canceled_with_one_scancel(
    spec: _WorldSpec, delay_s: float
) -> None:
    """Stopped while queued, running, tearing down or already ended: one scancel, no error."""
    asyncio.run(_stopped_after(spec, delay_s))


def _stuck_teardown() -> SchedulerTrace:
    return SchedulerTrace(
        name="stuck-teardown",
        provenance=(
            "a cancelled job that stays in COMPLETING past the confirmation bound "
            "while accounting has not yet recorded its end"
        ),
        steps=(
            TraceStep(
                at_seconds=0.0,
                queue_state="COMPLETING",
                accounting_state="RUNNING",
                reason="None",
            ),
            TraceStep(
                at_seconds=_FOREVER,
                queue_state=None,
                accounting_state="CANCELLED+",
                exit_code="0:0",
            ),
        ),
    )


_UNCONFIRMED = (
    _WorldSpec(
        "fake",
        lambda root: _fake_world(root, completing_s=_FOREVER, run_s=_FOREVER, queue_wait_s=0.0),
    ),
    _WorldSpec(
        "replay:running-forever+stuck-teardown",
        lambda root: _replay_world(root, SchedulerTrace.running_forever(), _stuck_teardown()),
    ),
)


@pytest.mark.parametrize("spec", _UNCONFIRMED, ids=lambda spec: spec.label)
@pytest.mark.asyncio
async def test_a_stop_beyond_the_confirmation_bound_is_canceling_and_later_confirmed(
    spec: _WorldSpec, tmp_path: Path
) -> None:
    """The job is known, so the stop is a typed CANCELING, never an unknown-identity error."""
    world = spec.build(tmp_path)
    executor, coordinator = _stack(world)
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(world.cluster.accepted.wait)
    record = await coordinator.cancel(handle.id)
    assert record.state is EvaluationState.CANCELING
    assert record.cancel_requested
    assert world.cluster.scancels == 1
    # While the job tears down, confirming the stop must not send further scancels.
    assert world.transport_scancels() <= 1
    world.clock.advance(_FOREVER)
    record = await coordinator.snapshot(handle.id)
    assert record.state is EvaluationState.CANCELED
    await executor.close()


async def _finish_time(spec: _WorldSpec) -> tuple[float, float]:
    """Clock when the evaluation succeeded, and when accounting said its job ended."""
    with tempfile.TemporaryDirectory() as raw:
        world = spec.build(Path(raw))
        executor, coordinator = _stack(world)
        handle = await coordinator.submit(_request())
        await asyncio.to_thread(world.cluster.accepted.wait)
        submitted_at = world.clock.now()
        for _ in range(10_000):
            record = await coordinator.snapshot(handle.id)
            if record.state is EvaluationState.SUCCEEDED:
                break
            await executor.wait_for_change(handle.id, 60.0)
        finished = world.clock.now()
        await executor.close()
        return finished, submitted_at


_ENDED_PROMPTLY = tuple(
    spec for spec in WORLDS if spec.label == "replay:pending-then-running+cancel-running"
)


@pytest.mark.parametrize("spec", _ENDED_PROMPTLY, ids=lambda spec: spec.label)
def test_an_evaluation_ends_when_accounting_reports_the_job_ended(spec: _WorldSpec) -> None:
    """The queue keeps a finished job in COMPLETING for 23 to 41 s; waiting for it wastes that."""
    lifetime = LIFETIMES[spec.label.split(":")[1].split("+")[0]]
    finished, submitted_at = asyncio.run(_finish_time(spec))
    # Submission returns a few commands after sbatch; one poll interval plus a few commands
    # separate the job's end from the poll that observes it. The recorded COMPLETING
    # lag (35 s) is longer than this, so waiting for the queue to forget the job fails.
    slack = 25.0
    assert finished <= submitted_at + lifetime.ended_at_seconds + slack


async def _stages_reported_while_running(spec: _WorldSpec) -> list[str]:
    with tempfile.TemporaryDirectory() as raw:
        world = spec.build(Path(raw))
        parked = threading.Event()
        # The submitting executor's own waiting is parked, so this test alone moves time.

        def park(_seconds: float) -> None:
            parked.wait()

        submitter, coordinator = _stack(world, pause=park)
        handle = await coordinator.submit(_request())
        await asyncio.to_thread(world.cluster.accepted.wait)
        reader = _executor(world)
        seen: list[str] = []
        try:
            for _ in range(10_000):
                world.clock.advance(5.0)
                polled = await reader.poll(handle.id)
                if polled.phase is PollPhase.ENDED:
                    break
                if polled.phase is PollPhase.RUNNING and polled.current_stage is not None:
                    seen.append(polled.current_stage)
        finally:
            parked.set()
            await submitter.close()
        return seen


# A 12.9 s run is shorter than the commands that submit it, so it cannot be sampled.
_LONG_RUNS = tuple(
    spec for spec in WORLDS if spec.label == "fake" or "pending-then-running" in spec.label
)


@pytest.mark.parametrize("spec", _LONG_RUNS, ids=lambda spec: spec.label)
def test_the_reported_stage_follows_the_stage_that_is_running(spec: _WorldSpec) -> None:
    """A fused job shows each of its stages in order, not the first stage throughout."""
    seen = asyncio.run(_stages_reported_while_running(spec))
    order = [stage.name for stage in _request().stages]
    indexes = [order.index(name) for name in seen]
    assert indexes == sorted(indexes)
    assert seen[0] == order[0]
    assert seen[-1] == order[-1]


_FAULT_WORLDS = tuple(
    spec for spec in WORLDS if spec.label.startswith("replay:pending-then-running")
)
# A poll or a cancel sends fewer commands than this, so the later positions are
# no-fault controls.
_FAULT_POSITIONS = 12


def _after_submitter_idles(
    world: _World,
) -> tuple[SlurmEvaluationExecutor, EvaluationCoordinator, threading.Event, threading.Event]:
    """A stack whose submitter parks at its first wait, so a test alone sends commands.

    The first pause is the submitter's own wait loop and parks; every later pause
    (a cancel confirming) advances the world's clock.
    """
    idle = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    parked = []

    def pause(seconds: float) -> None:
        with lock:
            first = not parked
            parked.append(True)
        if first:
            idle.set()
            release.wait()
        else:
            world.clock.advance(seconds)

    executor, coordinator = _stack(world, pause)
    return executor, coordinator, idle, release


async def _poll_with_dropped_command(spec: _WorldSpec, delay_s: float, position: int) -> None:
    with tempfile.TemporaryDirectory() as raw:
        world = spec.build(Path(raw))
        assert world.faults is not None
        submitter, coordinator, idle, release = _after_submitter_idles(world)
        handle = await coordinator.submit(_request())
        await asyncio.to_thread(idle.wait)
        reader = _executor(world)
        try:
            world.clock.advance(delay_s)
            world.faults.drop_command(position)
            polled = await reader.poll(handle.id)
        finally:
            release.set()
            await submitter.close()
        # Every fault is one transient transport error and the job is known: the
        # other source (queue or accounting) answers. Ended jobs also collect files,
        # which is a different boundary.
        if polled.phase is not PollPhase.ENDED:
            assert polled.phase is not PollPhase.UNKNOWN, polled.detail


@pytest.mark.parametrize("spec", _FAULT_WORLDS, ids=lambda spec: spec.label)
@_PROPERTY
@example(delay_s=10.0, position=1)
@example(delay_s=150.0, position=0)
@example(delay_s=150.0, position=1)
@example(delay_s=270.0, position=0)
@example(delay_s=270.0, position=1)
@example(delay_s=270.0, position=2)
@given(
    delay_s=st.floats(0, 330, allow_nan=False),
    position=st.integers(0, _FAULT_POSITIONS),
)
def test_a_dropped_command_never_makes_a_poll_of_a_known_job_unknown(
    spec: _WorldSpec, delay_s: float, position: int
) -> None:
    """A lost squeue, sacct or stage read while queued, running or tearing down is survivable."""
    asyncio.run(_poll_with_dropped_command(spec, delay_s, position))


async def _cancel_with_dropped_command(spec: _WorldSpec, delay_s: float, position: int) -> None:
    with tempfile.TemporaryDirectory() as raw:
        world = spec.build(Path(raw))
        assert world.faults is not None
        executor, coordinator, idle, release = _after_submitter_idles(world)
        handle = await coordinator.submit(_request())
        await asyncio.to_thread(idle.wait)
        try:
            world.clock.advance(delay_s)
            world.faults.drop_command(position)
            # The job id is known, so the stop is never an error: it ends or it is CANCELING.
            record = await coordinator.cancel(handle.id)
            assert record.state in {
                EvaluationState.CANCELED,
                EvaluationState.CANCELING,
                EvaluationState.SUCCEEDED,
            }
            assert world.transport_scancels() <= 1
            world.clock.advance(_FOREVER)
            record = await coordinator.snapshot(handle.id)
            assert record.state in {EvaluationState.CANCELED, EvaluationState.SUCCEEDED}
        finally:
            release.set()
            await executor.close()


@pytest.mark.parametrize("spec", _FAULT_WORLDS, ids=lambda spec: spec.label)
@_PROPERTY
@example(delay_s=10.0, position=0)
@example(delay_s=10.0, position=1)
@example(delay_s=10.0, position=2)
@example(delay_s=150.0, position=0)
@example(delay_s=150.0, position=1)
@example(delay_s=150.0, position=2)
@example(delay_s=150.0, position=3)
@example(delay_s=270.0, position=1)
@example(delay_s=270.0, position=2)
@given(
    delay_s=st.floats(0, 330, allow_nan=False),
    position=st.integers(0, _FAULT_POSITIONS),
)
def test_a_dropped_command_never_makes_the_stop_of_a_known_job_an_error(
    spec: _WorldSpec, delay_s: float, position: int
) -> None:
    """A lost command at any step of a stop leaves it CANCELING at worst, then confirmed."""
    asyncio.run(_cancel_with_dropped_command(spec, delay_s, position))
