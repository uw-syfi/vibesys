"""The Slurm executor bounds queue wait and run time separately.

A live run's job waited 3 h 56 min in the queue, started, and was cancelled 3.5
minutes later because one deadline counted from submission. Here a job's run
budget starts when it is first seen running, and queue wait is bounded only by
``queue_timeout_seconds``. Each test drives a Fake scheduler whose jobs follow
the same virtual clock the executor sleeps on, so the whole wait runs in no
real time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    ExecutorObservation,
    is_finished,
)
from vs_sandbox.api.slurm import SlurmEvaluationExecutor, SlurmLedgerAttachment, SlurmStagePayload
from vs_sim.api.testing import NO_PROCESSES, InlineBlockingRunner, ManualClock
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitOutcome,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerPolicy,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

RUN_LIMIT_S = 4 * 3600  # the live run's job_timeout_seconds
POLL_S = 60.0
# Only guards a hang: every evaluation here ends on the virtual clock.
_BOUND_S = 60.0
PROJECT = "demo-0123456789ab"
_BLOCKING = InlineBlockingRunner()


def _config(**update: object) -> SlurmConfig:
    return SlurmConfig(
        name="fake-cluster",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="fake-cluster"),
        poll_interval_seconds=POLL_S,
        job_timeout_seconds=RUN_LIMIT_S,
    ).model_copy(update=update)


class _Scheduler(FakeCluster):
    """Every batch queues for ``queue_s`` then runs for ``run_s`` on the clock, and succeeds."""

    def __init__(self, clock: ManualClock, *, queue_s: float, run_s: float) -> None:
        super().__init__(clock=clock)
        self.queue_s = queue_s
        self.run_s = run_s
        self.operations: list[str] = []

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if isinstance(request, SlurmBatchRequest) and operation_id not in self.operations:
            self.operations.append(operation_id)
            self.script(
                operation_id,
                queue_wait_s=self.queue_s,
                run_s=self.run_s,
                completing_s=0.0,
                result=SlurmBatchResult(
                    job_id="0",
                    job_exit_code=0,
                    job_output="",
                    stages=tuple(
                        SlurmBatchStageResult(
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

    def status(self) -> SlurmJobStatus:
        (operation,) = self.operations
        observed = self.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        return observed.status


def _request() -> EvaluationRequest:
    payload = SlurmStagePayload(command="run-benchmark", timeout_seconds=60)
    return EvaluationRequest(
        key="fused",
        owner_scope="baseline",
        stages=(EvaluationStep(name="benchmark", payload=payload.model_dump(mode="json")),),
    )


def _executor(
    tmp_path: Path, clock: ManualClock, scheduler: _Scheduler, config: SlurmConfig, *, ledger: bool
) -> SlurmEvaluationExecutor:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "bench.py").write_text("print('bench')", encoding="utf-8")
    attachment = SlurmLedgerAttachment(
        ledger=FakeEvaluationLedger(project=PROJECT, clock=ManualClock()),
        config=config,
        project=PROJECT,
        holder="run-1",
        policy=LedgerPolicy(),
        clock=clock,
    )
    return SlurmEvaluationExecutor(
        config,
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=tmp_path / "handles",
        cluster=scheduler,
        clock=clock,
        sleeper=clock,
        blocking=_BLOCKING,
        processes=NO_PROCESSES,
        ledger=attachment if ledger else None,
        ledger_candidate="0f3c9e1" if ledger else None,
    )


async def _finished(executor: SlurmEvaluationExecutor, handle_id: str) -> ExecutorObservation:
    """The evaluation's own terminal publication; the bound only guards a hang."""
    while True:
        observed = await executor.inspect(handle_id)
        if observed is not None and is_finished(observed.state):
            return observed
        await executor.wait_for_change(handle_id, timeout_s=_BOUND_S)


@pytest.mark.parametrize("ledger", [False, True], ids=["direct", "ledger"])
@pytest.mark.parametrize(
    "queue_s",
    # The live failure (3 h 56 min), a start just before and just after the old
    # submission-anchored deadline, and a queue wait of three run budgets.
    [0.0, 14_160.0, RUN_LIMIT_S - POLL_S / 2, RUN_LIMIT_S + POLL_S / 2, 3.0 * RUN_LIMIT_S],
)
async def test_a_job_that_starts_late_still_gets_its_whole_run_budget(
    tmp_path: Path, queue_s: float, *, ledger: bool
) -> None:
    clock = ManualClock(1_000.0)
    # Runs for most of its budget: past any deadline counted from submission.
    scheduler = _Scheduler(clock, queue_s=queue_s, run_s=RUN_LIMIT_S - 2 * POLL_S)
    executor = _executor(tmp_path, clock, scheduler, _config(), ledger=ledger)

    await executor.submit(_request(), handle_id="eval-1")
    observed = await _finished(executor, "eval-1")

    assert observed.state is EvaluationState.SUCCEEDED, observed.failure
    assert scheduler.status() is SlurmJobStatus.COMPLETED
    await executor.close()


async def test_a_job_that_runs_past_its_run_budget_fails_and_is_cancelled(
    tmp_path: Path,
) -> None:
    clock = ManualClock(1_000.0)
    scheduler = _Scheduler(clock, queue_s=14_160.0, run_s=2.0 * RUN_LIMIT_S)
    executor = _executor(tmp_path, clock, scheduler, _config(), ledger=False)

    await executor.submit(_request(), handle_id="eval-1")
    observed = await _finished(executor, "eval-1")

    assert observed.state is EvaluationState.FAILED
    assert observed.failure is not None
    assert f"its {RUN_LIMIT_S}-second run deadline" in observed.failure
    # Failed within one poll of the run budget, counted from the start.
    assert 14_160.0 + RUN_LIMIT_S <= clock.now() - 1_000.0 <= 14_160.0 + RUN_LIMIT_S + 2 * POLL_S
    assert scheduler.status() is SlurmJobStatus.CANCELLED
    await executor.close()


async def test_queue_wait_is_bounded_only_by_the_queue_timeout(tmp_path: Path) -> None:
    clock = ManualClock(1_000.0)
    scheduler = _Scheduler(clock, queue_s=10.0 * RUN_LIMIT_S, run_s=60.0)
    config = _config(queue_timeout_seconds=6 * 3600)
    executor = _executor(tmp_path, clock, scheduler, config, ledger=False)

    await executor.submit(_request(), handle_id="eval-1")
    observed = await _finished(executor, "eval-1")

    assert observed.state is EvaluationState.FAILED
    assert observed.failure is not None
    assert "its 21600-second queue deadline" in observed.failure
    assert 6 * 3600 <= clock.now() - 1_000.0 <= 6 * 3600 + POLL_S
    assert scheduler.status() is SlurmJobStatus.CANCELLED
    await executor.close()
