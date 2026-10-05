"""Slurm evaluation lifecycle under scheduler queueing and teardown lag.

The Fake scheduler reproduces two behaviors of a real Slurm cluster: a job
waits PENDING after submit, and a finished or cancelled job stays in COMPLETING
(reported as RUNNING) for a while before its terminal state appears.
"""

from __future__ import annotations

import asyncio
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
    ExecutorCancellationUnconfirmedError,
    ExecutorCancellationUnknownError,
    PollPhase,
)
from vs_evaluation.api.testing import FakeClock
from vs_sandbox.api.slurm import SlurmEvaluationExecutor, SlurmStagePayload
from vs_slurm.api import (
    ClusterCancelOutcome,
    ClusterSubmitOutcome,
    ClusterTarget,
    FakeCluster,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmSshTransport,
)
from vs_slurm.api import SlurmBatchStageResult as _StageResult

_CONFIRMATIONS = 4
_RANK = {
    EvaluationState.QUEUED: 0,
    EvaluationState.STARTING: 1,
    EvaluationState.RUNNING: 2,
    EvaluationState.SUCCEEDED: 3,
    EvaluationState.FAILED: 3,
    EvaluationState.CANCELED: 3,
}
_PHASE_RANK = {
    PollPhase.UNSUBMITTED: 0,
    PollPhase.QUEUED: 1,
    PollPhase.RUNNING: 2,
    PollPhase.ENDED: 3,
}


@dataclass(frozen=True)
class _Schedule:
    """A monotone scheduler sequence: PENDING*a, RUNNING*b, COMPLETING*c, terminal."""

    pending: int
    running: int
    completing: int
    terminal: SlurmJobStatus = SlurmJobStatus.COMPLETED


class _ScheduledCluster(FakeCluster):
    """Fake whose every batch follows one schedule and that counts scancel calls."""

    def __init__(self, schedule: _Schedule) -> None:
        super().__init__()
        self._schedule = schedule
        self.scancels = 0
        self.accepted = threading.Event()

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
                states=(SlurmJobStatus.RUNNING,) * self._schedule.running
                + (self._schedule.terminal,),
                pending_polls=self._schedule.pending,
                teardown_lag=self._schedule.completing,
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
        poll_interval_seconds=1.0,
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
    root: Path, cluster: FakeCluster
) -> tuple[SlurmEvaluationExecutor, EvaluationCoordinator]:
    workspace = root / "workspace"
    workspace.mkdir()
    config = _config()
    executor = SlurmEvaluationExecutor(
        config,
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        cluster=cluster,
        # Pacing is injected: the loops run as fast as the Fake answers.
        pause=lambda _seconds: None,
        cancel_confirmation_seconds=_CONFIRMATIONS * config.poll_interval_seconds,
    )
    store = evaluation_testing.InMemoryEvaluationStore()
    return executor, EvaluationCoordinator(executor, store, FakeClock())


_OPS = st.lists(st.sampled_from(("snapshot", "inspect_only", "poll", "wait")), max_size=40)


async def _publishes_monotonically(schedule: _Schedule, operations: list[str]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        executor, coordinator = _stack(Path(raw), _ScheduledCluster(schedule))
        handle = await coordinator.submit(_request())
        states: list[int] = []
        phases: list[int] = []

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
                        phases.append(_PHASE_RANK[polled.phase])
                case _:
                    await executor.wait_for_change(handle.id, 0.001)
            return await coordinator.recorded_status(handle.id)

        for operation in operations:
            await step(operation)
        for _ in range(100_000):
            if await step("snapshot") is EvaluationState.SUCCEEDED:
                break
            await executor.wait_for_change(handle.id, 0.001)
        assert states[-1] == _RANK[EvaluationState.SUCCEEDED]
        assert states == sorted(states)
        assert phases == sorted(phases)
        await executor.close()


@settings(max_examples=30)
@given(
    pending=st.integers(0, 6),
    running=st.integers(0, 6),
    completing=st.integers(0, 6),
    terminal=st.sampled_from((SlurmJobStatus.COMPLETED,)),
    operations=_OPS,
)
def test_published_lifecycle_never_decreases_for_any_monotone_scheduler_sequence(
    pending: int,
    running: int,
    completing: int,
    terminal: SlurmJobStatus,
    operations: list[str],
) -> None:
    """Queueing and teardown readings in any interleaving never regress or error."""
    asyncio.run(
        _publishes_monotonically(_Schedule(pending, running, completing, terminal), operations)
    )


async def _stopped_during_teardown(lag: int) -> None:
    with tempfile.TemporaryDirectory() as raw:
        # Never exits by itself: only the cancellation ends the job.
        cluster = _ScheduledCluster(
            _Schedule(pending=0, running=1, completing=lag, terminal=SlurmJobStatus.RUNNING)
        )
        executor, coordinator = _stack(Path(raw), cluster)
        handle = await coordinator.submit(_request())
        await asyncio.to_thread(cluster.accepted.wait)
        record = await coordinator.cancel(handle.id)
        assert record.state is EvaluationState.CANCELED
        assert cluster.scancels == 1
        await executor.close()
        assert cluster.scancels == 1


@settings(max_examples=_CONFIRMATIONS)
@given(lag=st.integers(0, _CONFIRMATIONS - 1))
def test_a_user_stop_is_canceled_after_any_teardown_lag_within_the_bound(lag: int) -> None:
    """One scancel, then CANCELED once COMPLETING ends inside the confirmation wait."""
    asyncio.run(_stopped_during_teardown(lag))


@pytest.mark.asyncio
async def test_a_stop_beyond_the_confirmation_bound_is_unconfirmed_not_unknown(
    tmp_path: Path,
) -> None:
    """The job is known, so the outcome names it and a later cancel reconciles it."""
    cluster = _ScheduledCluster(
        _Schedule(pending=0, running=1, completing=10**9, terminal=SlurmJobStatus.RUNNING)
    )
    executor, coordinator = _stack(tmp_path, cluster)
    handle = await coordinator.submit(_request())
    await asyncio.to_thread(cluster.accepted.wait)
    with pytest.raises(ExecutorCancellationUnconfirmedError) as unconfirmed:
        await coordinator.cancel(handle.id)
    assert not isinstance(unconfirmed.value, ExecutorCancellationUnknownError)
    assert unconfirmed.value.job_id
    assert cluster.scancels == 1
    record = await coordinator.recorded_snapshot(handle.id)
    assert record.cancel_requested
    assert record.state is not EvaluationState.CANCELED
    await executor.close()
