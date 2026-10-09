"""Slurm evaluation lifecycle under scheduler queueing, requeue and teardown lag.

The Fake scheduler reproduces three behaviors of a real Slurm cluster as a
function of its own clock: a job waits PENDING after submit, the scheduler may
requeue it (a new attempt), and a finished or cancelled job stays in COMPLETING
(reported as RUNNING) for a while before its terminal state appears. Time moves
only when the executor paces itself, so nothing here sleeps.
"""

from __future__ import annotations

import asyncio
import math
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given, settings
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
    ClusterCancelOutcome,
    ClusterSubmitOutcome,
    ClusterTarget,
    FakeCluster,
    ManualClock,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmConfig,
    SlurmJobRequest,
    SlurmSshTransport,
)
from vs_slurm.api import SlurmBatchStageResult as _StageResult

_POLL_INTERVAL_S = 1.0
# The executor waits this long for a cancelled job to end before it leaves the
# evaluation CANCELING. It exceeds the profile's longest COMPLETING time.
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


@dataclass(frozen=True)
class _Schedule:
    """One job's timeline in seconds on the Fake's clock."""

    queue_wait_s: float
    run_s: float
    completing_s: float
    requeues: int = 0


class _ScheduledCluster(FakeCluster):
    """Fake whose every batch follows one schedule and that counts scancel calls."""

    def __init__(self, schedule: _Schedule) -> None:
        super().__init__(clock=ManualClock())
        self._schedule = schedule
        self.scancels = 0
        self.accepted = threading.Event()

    def advance(self, seconds: float) -> None:
        """Move the scheduler's clock; the executor's pacing calls this."""
        assert isinstance(self.clock, ManualClock)
        self.clock.advance(seconds)

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if isinstance(request, SlurmBatchRequest):
            stages = tuple(
                _StageResult(
                    name=stage.name,
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                    elapsed_seconds=1.0,
                    skipped=False,
                )
                for stage in request.stages
            )
            self.script(
                operation_id,
                queue_wait_s=self._schedule.queue_wait_s,
                run_s=self._schedule.run_s,
                completing_s=self._schedule.completing_s,
                requeues=self._schedule.requeues,
                result=SlurmBatchResult(
                    job_id="0",
                    job_exit_code=0,
                    job_output="",
                    stages=stages,
                    phase_timings_seconds={},
                    content_cache_hits=0,
                ),
            )
        outcome = super().submit(request, operation_id=operation_id)
        self.accepted.set()
        return outcome

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        self.scancels += 1
        return super().cancel(target, by_job_id=by_job_id)


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="fake-cluster",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="fake-cluster"),
        poll_interval_seconds=_POLL_INTERVAL_S,
    )


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        key="lifecycle",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(command=f"run-{name}", timeout_seconds=5).model_dump(
                    mode="json"
                ),
            )
            for name in ("accuracy", "benchmark")
        ),
    )


def _stack(
    root: Path, cluster: _ScheduledCluster
) -> tuple[SlurmEvaluationExecutor, EvaluationCoordinator]:
    workspace = root / "workspace"
    workspace.mkdir()
    executor = SlurmEvaluationExecutor(
        _config(),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        cluster=cluster,
        # Pacing is the Fake's clock: waiting for the scheduler advances it.
        pause=cluster.advance,
        cancel_confirmation_seconds=_CONFIRMATION_S,
    )
    store = evaluation_testing.InMemoryEvaluationStore()
    return executor, EvaluationCoordinator(executor, store, FakeClock())


_OPS = st.lists(st.sampled_from(("snapshot", "inspect_only", "poll", "wait")), max_size=40)
_SECONDS = st.floats(0, 100, allow_nan=False)


async def _publishes_monotonically(schedule: _Schedule, operations: list[str]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        executor, coordinator = _stack(Path(raw), _ScheduledCluster(schedule))
        handle = await coordinator.submit(_request())
        states: list[int] = []
        # Phases are ordered per attempt: a requeue may drop RUNNING back to QUEUED.
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
        # Each publication wakes the wait, so this ends when the job does; the
        # bound only turns a lost wake-up into a failure instead of a hang.
        for _ in range(10_000):
            if await step("snapshot") is EvaluationState.SUCCEEDED:
                break
            await executor.wait_for_change(handle.id, 60.0)
        assert states[-1] == _RANK[EvaluationState.SUCCEEDED]
        assert states == sorted(states)
        assert phases == sorted(phases)
        await executor.close()


@settings(max_examples=30)
@given(
    queue_wait=_SECONDS,
    run=_SECONDS,
    completing=_SECONDS,
    requeues=st.integers(0, 2),
    operations=_OPS,
)
def test_published_lifecycle_never_decreases_for_any_scheduler_timeline(
    queue_wait: float, run: float, completing: float, requeues: int, operations: list[str]
) -> None:
    """Queueing, requeue and teardown readings in any interleaving never regress or error."""
    asyncio.run(
        _publishes_monotonically(_Schedule(queue_wait, run, completing, requeues), operations)
    )


def _parked_submitter(
    cluster: _ScheduledCluster, root: Path
) -> tuple[SlurmEvaluationExecutor, EvaluationCoordinator, threading.Event, threading.Event]:
    """A stack whose submitter parks at its first wait, so a test alone acts on the job.

    Acting on ``cluster.accepted`` alone races the submitter's remaining acceptance
    work: a stop landing mid-acceptance sends its own scancel, and a second one when
    the confirmation budget runs out. Parking leaves the batch accepted and its handle
    recorded. Every later pause (a cancel confirming) advances the cluster's clock.
    """
    idle = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    parked: list[bool] = []

    def pause(seconds: float) -> None:
        with lock:
            first = not parked
            parked.append(True)
        if first:
            idle.set()
            release.wait()
        else:
            cluster.advance(seconds)

    workspace = root / "workspace"
    workspace.mkdir()
    executor = SlurmEvaluationExecutor(
        _config(),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        cluster=cluster,
        pause=pause,
        cancel_confirmation_seconds=_CONFIRMATION_S,
    )
    store = evaluation_testing.InMemoryEvaluationStore()
    return executor, EvaluationCoordinator(executor, store, FakeClock()), idle, release


async def _stopped_while_active(schedule: _Schedule) -> None:
    with tempfile.TemporaryDirectory() as raw:
        cluster = _ScheduledCluster(schedule)
        executor, coordinator, idle, release = _parked_submitter(cluster, Path(raw))
        handle = await coordinator.submit(_request())
        await asyncio.to_thread(idle.wait)
        try:
            record = await coordinator.cancel(handle.id)
            assert record.state is EvaluationState.CANCELED
            assert cluster.scancels == 1
        finally:
            release.set()
            await executor.close()
        assert cluster.scancels == 1


@settings(max_examples=30)
@given(
    queue_wait=st.floats(0, 95, allow_nan=False),
    completing=st.floats(0, _CONFIRMATION_S - 1, allow_nan=False),
)
def test_a_user_stop_is_canceled_for_any_queue_wait_and_teardown_within_the_bound(
    queue_wait: float, completing: float
) -> None:
    """One scancel, then CANCELED, whether the job was queued or running when stopped."""
    asyncio.run(
        _stopped_while_active(
            _Schedule(queue_wait_s=queue_wait, run_s=math.inf, completing_s=completing)
        )
    )


@pytest.mark.asyncio
async def test_a_stop_beyond_the_confirmation_bound_leaves_the_evaluation_canceling(
    tmp_path: Path,
) -> None:
    """The job is known, so the evaluation is CANCELING, and a later request confirms it."""
    cluster = _ScheduledCluster(_Schedule(queue_wait_s=0, run_s=math.inf, completing_s=10**9))
    executor, coordinator, idle, release = _parked_submitter(cluster, tmp_path)
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(idle.wait)
    try:
        record = await coordinator.cancel(handle.id)
        assert record.state is EvaluationState.CANCELING
        assert record.cancel_requested
        assert cluster.scancels == 1
        # The scheduler finishes tearing the job down; the next request observes it.
        cluster.advance(10**9)
        record = await coordinator.snapshot(handle.id)
        assert record.state is EvaluationState.CANCELED
    finally:
        release.set()
        await executor.close()


@pytest.mark.asyncio
async def test_recovery_of_a_stopped_job_ends_canceled_like_a_poll(tmp_path: Path) -> None:
    """Both read paths agree: a cancelled job is CANCELED even though its script exited 0.

    A stop that outlives the confirmation bound leaves the evaluation CANCELING. The
    recovery task a later ``inspect`` starts reads the job's terminal state; it used to
    collect the job's evidence, where exit code 0 contradicted CANCELLED and the stop
    ended FAILED, while ``poll`` reported CANCELED for the same reading.
    """
    cluster = _ScheduledCluster(_Schedule(queue_wait_s=0, run_s=math.inf, completing_s=10**9))
    executor, coordinator, idle, release = _parked_submitter(cluster, tmp_path)
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(idle.wait)
    try:
        record = await coordinator.cancel(handle.id)
        assert record.state is EvaluationState.CANCELING
        cluster.advance(10**9)
        polled = await executor.poll(handle.id)
        assert polled.terminal is not None
        assert polled.terminal.state is EvaluationState.CANCELED
        observed = await executor.inspect(handle.id)
        for _ in range(10_000):
            assert observed is not None
            if observed.state in {
                EvaluationState.SUCCEEDED,
                EvaluationState.FAILED,
                EvaluationState.CANCELED,
            }:
                break
            await executor.wait_for_change(handle.id, 60.0)
            observed = await executor.inspect(handle.id)
        assert observed is not None
        assert observed.state is EvaluationState.CANCELED
    finally:
        release.set()
        await executor.close()


@pytest.mark.parametrize("requeues", [1, 2])
@pytest.mark.asyncio
async def test_an_ended_poll_names_the_attempt_that_ended(tmp_path: Path, requeues: int) -> None:
    """Polls are ordered per attempt, so the ending poll carries the last attempt, not attempt 0."""
    schedule = _Schedule(queue_wait_s=1.0, run_s=1.0, completing_s=1.0, requeues=requeues)
    cluster = _ScheduledCluster(schedule)
    executor, coordinator, idle, release = _parked_submitter(cluster, tmp_path)
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(idle.wait)
    phases: list[tuple[int, int]] = []
    try:
        for _ in range(1_000):
            cluster.advance(0.5)
            polled = await executor.poll(handle.id)
            if polled.phase in _PHASE_RANK:
                phases.append((polled.attempt, _PHASE_RANK[polled.phase]))
            if polled.phase is PollPhase.ENDED:
                break
    finally:
        release.set()
        await executor.close()
    assert phases[-1][1] == _PHASE_RANK[PollPhase.ENDED]
    assert phases[-1][0] == requeues
    assert phases == sorted(phases)
