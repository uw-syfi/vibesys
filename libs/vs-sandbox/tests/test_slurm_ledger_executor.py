"""The Slurm executor with the evaluation ledger (#2757): restarts reattach, nothing is cancelled.

Each test runs one or two "runs" (executors with their own run directory and
lease holder) against one Fake scheduler and one Fake ledger. The scheduler's
jobs follow its clock, which only the test advances. Each run's wait loop parks
in its first poll sleep, so the test drives every later step through ``poll``,
the read the core makes, and nothing happens behind its back.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    ExecutorObservation,
    PollPhase,
    is_finished,
)
from vs_evaluation.api.testing import wait_until_executor_started
from vs_sandbox.api.slurm import (
    SlurmEvaluationExecutor,
    SlurmLedgerAttachment,
    SlurmStagePayload,
)
from vs_sim.api import yield_now
from vs_sim.api.testing import ManualClock
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitOutcome,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerPolicy,
    LedgerState,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmSshTransport,
    Withdrawn,
    ledger_operation_id,
    withdraw,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

PROJECT = "demo-0123456789ab"
LEASE_S = 300.0
GRACE_S = 1800.0
# Only guards a hang: every wait below ends on an event the test already caused.
_BOUND_S = 60.0
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
    poll_interval_seconds=1.0,
)


class _ParkingSleeper:
    """The first sleep of a run's wait loop parks until the run closes."""

    def __init__(self) -> None:
        self.parked = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        del seconds
        self.parked.set()
        await asyncio.Event().wait()


_DIED = "the submitting process died before sbatch"


def _die() -> None:
    raise OSError(_DIED)


class _Scheduler(FakeCluster):
    """A scheduler whose every batch queues, runs and succeeds on its clock; it records each job."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.jobs: set[str] = set()
        self.queue_s = 100.0
        self.run_s = 100.0
        # How many submissions die before sbatch, as a submitter process that crashed would.
        self.crash_submits = 0

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if self.crash_submits:
            # The submitter stages the job and dies before sbatch: the scheduler side
            # holds a half-done submission under the name, and no job.
            self.crash_submits -= 1
            self.script(operation_id, on_dispatch=_die)
            return super().submit(request, operation_id=operation_id)
        if isinstance(request, SlurmBatchRequest) and operation_id not in self.jobs:
            self.jobs.add(operation_id)
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

    def status(self, operation_id: str) -> SlurmJobStatus:
        observed = self.inspect(operation_id)
        assert isinstance(observed, ClusterObservation)
        return observed.status


class _ClockSleeper:
    """Every sleep moves the shared clock on by its length: time passes only by waiting."""

    def __init__(self, clock: ManualClock) -> None:
        self.parked = asyncio.Event()
        self._clock = clock

    async def sleep(self, seconds: float) -> None:
        self._clock.advance(max(seconds, 0.0))
        await yield_now()


@dataclass
class _Run:
    executor: SlurmEvaluationExecutor
    sleeper: _ParkingSleeper | _ClockSleeper

    async def parked(self, handle_id: str) -> None:
        """Return once the evaluation's wait loop sleeps; fail if the evaluation ends first."""
        await wait_until_executor_started(self.sleeper.parked, self.executor, handle_id)

    async def terminal(self, handle_id: str) -> ExecutorObservation:
        """The evaluation's terminal observation, as the core's poll reads it."""
        polled = await self.executor.poll(handle_id)
        assert polled.phase is PollPhase.ENDED, polled
        assert polled.terminal is not None
        return polled.terminal


@dataclass
class _World:
    clock: ManualClock = field(default_factory=lambda: ManualClock(1000.0))
    ledger: FakeEvaluationLedger = field(
        default_factory=lambda: FakeEvaluationLedger(project=PROJECT)
    )
    scheduler: _Scheduler = field(init=False)

    def __post_init__(self) -> None:
        self.scheduler = _Scheduler(self.clock)

    def run(
        self,
        root: Path,
        holder: str,
        *,
        content: str = "print('bench')",
        sleeper: _ParkingSleeper | _ClockSleeper | None = None,
        claim_seconds: float = 900.0,
    ) -> _Run:
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "bench.py").write_text(content, encoding="utf-8")
        sleeper = sleeper or _ParkingSleeper()
        executor = SlurmEvaluationExecutor(
            CONFIG,
            workspace=workspace,
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=root / "handles",
            cluster=self.scheduler,
            clock=self.clock,
            sleeper=sleeper,
            ledger=SlurmLedgerAttachment(
                ledger=self.ledger.reopen(),
                config=CONFIG,
                project=PROJECT,
                holder=holder,
                policy=LedgerPolicy(
                    lease_seconds=LEASE_S, grace_seconds=GRACE_S, claim_seconds=claim_seconds
                ),
                clock=self.clock,
            ),
            ledger_candidate="0f3c9e1",
        )
        return _Run(executor, sleeper)

    def only_key(self) -> str:
        (key,) = self.ledger.list_keys()
        return key

    def entry_state(self) -> LedgerState:
        entry = self.ledger.read(self.only_key())
        assert entry is not None
        return entry.record.state


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        key="fused",
        owner_scope="baseline",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(command=f"run-{name}", timeout_seconds=60).model_dump(
                    mode="json"
                ),
            )
            for name in ("accuracy", "benchmark")
        ),
    )


async def _attached(run: _Run, handle_id: str) -> None:
    await run.executor.submit(_request(), handle_id=handle_id)
    await run.parked(handle_id)


async def _finished(executor: SlurmEvaluationExecutor, handle_id: str) -> ExecutorObservation:
    """The evaluation's own task's terminal publication; the bound only guards a hang."""
    while True:
        observed = await executor.inspect(handle_id)
        if observed is not None and is_finished(observed.state):
            return observed
        await executor.wait_for_change(handle_id, timeout_s=_BOUND_S)


@pytest.mark.parametrize("restart", ["resume", "relaunch"])
async def test_a_job_submitted_before_a_stop_is_the_same_job_after_a_restart(
    tmp_path: Path, restart: str
) -> None:
    world = _World()
    first = world.run(tmp_path / "run-1", "run-1")
    await _attached(first, "eval-1")
    (operation,) = world.scheduler.jobs

    await first.executor.close()
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING
    assert world.entry_state() is LedgerState.SUBMITTED

    if restart == "resume":
        second = world.run(tmp_path / "run-1", "run-1")
        handle_id = "eval-1"
        assert await second.executor.inspect(handle_id) is not None
    else:
        # A fresh relaunch: a new run directory, a new worktree path, the same content.
        second = world.run(tmp_path / "run-2" / "elsewhere", "run-2")
        handle_id = "eval-2"
        await second.executor.submit(_request(), handle_id=handle_id)
    await second.parked(handle_id)
    world.clock.advance(world.scheduler.queue_s + world.scheduler.run_s)
    assert (await second.terminal(handle_id)).state is EvaluationState.SUCCEEDED
    assert world.scheduler.jobs == {operation}
    await second.executor.close()


async def test_a_job_that_finished_while_vibesys_was_down_is_collected_not_rerun(
    tmp_path: Path,
) -> None:
    world = _World()
    first = world.run(tmp_path / "run-1", "run-1")
    await _attached(first, "eval-1")
    await first.executor.close()
    world.clock.advance(10 * (world.scheduler.queue_s + world.scheduler.run_s))

    second = world.run(tmp_path / "run-2", "run-2")
    assert (await second.executor.poll("eval-2")).phase is PollPhase.UNSUBMITTED
    await second.executor.submit(_request(), handle_id="eval-2")
    assert (await _finished(second.executor, "eval-2")).state is EvaluationState.SUCCEEDED
    assert len(world.scheduler.jobs) == 1
    await second.executor.close()


async def test_two_runs_asking_for_one_request_share_one_job(tmp_path: Path) -> None:
    world = _World()
    runs = [world.run(tmp_path / f"run-{index}", f"run-{index}") for index in range(2)]
    for index, run in enumerate(runs):
        await _attached(run, f"eval-{index}")
    world.clock.advance(world.scheduler.queue_s + world.scheduler.run_s)
    for index, run in enumerate(runs):
        assert (await run.terminal(f"eval-{index}")).state is EvaluationState.SUCCEEDED
        await run.executor.close()
    assert len(world.scheduler.jobs) == 1
    entry = world.ledger.read(world.only_key())
    assert entry is not None
    assert {lease.holder for lease in entry.leases} == {"run-0", "run-1"}


async def test_cancel_lets_go_of_the_job_and_ends_the_evaluation_for_this_run(
    tmp_path: Path,
) -> None:
    world = _World()
    run = world.run(tmp_path / "run-1", "run-1")
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs

    await run.executor.cancel("eval-1")

    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING
    entry = world.ledger.read(world.only_key())
    assert entry is not None
    assert entry.leases == ()
    restarted = world.run(tmp_path / "run-1", "run-1")
    assert (await restarted.terminal("eval-1")).state is EvaluationState.CANCELED
    await run.executor.close()


async def test_an_attached_run_observes_a_withdrawal_as_cancelled(tmp_path: Path) -> None:
    world = _World()
    run = world.run(tmp_path / "run-1", "run-1")
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs

    outcome = withdraw(world.ledger, world.scheduler, world.only_key(), reason="removed")

    assert isinstance(outcome, Withdrawn)
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED
    # The run reads the ledger again a third of a lease period after its last renewal.
    world.clock.advance(LEASE_S / 3)
    assert (await run.terminal("eval-1")).state is EvaluationState.CANCELED
    observed = await run.executor.inspect("eval-1")
    assert observed is not None
    assert observed.state is EvaluationState.CANCELED
    await run.executor.close()


async def test_a_job_no_run_leases_is_cancelled_by_another_runs_sweep_after_its_grace(
    tmp_path: Path,
) -> None:
    world = _World()
    world.scheduler.queue_s = math.inf
    stopped = world.run(tmp_path / "run-1", "run-1")
    await _attached(stopped, "eval-1")
    (abandoned,) = world.scheduler.jobs
    await stopped.executor.close()

    # Another run evaluates something else; each of its polls renews its own lease first.
    other = world.run(tmp_path / "run-2", "run-2", content="print('other')")
    await _attached(other, "eval-2")
    (live,) = world.scheduler.jobs - {abandoned}
    world.clock.advance(LEASE_S + GRACE_S - 1)
    assert (await other.executor.poll("eval-2")).phase is PollPhase.QUEUED
    assert world.scheduler.status(abandoned) is SlurmJobStatus.PENDING

    world.clock.advance(LEASE_S)
    assert (await other.executor.poll("eval-2")).phase is PollPhase.QUEUED
    assert world.scheduler.status(abandoned) is SlurmJobStatus.CANCELLED
    assert world.scheduler.status(live) is SlurmJobStatus.PENDING
    await other.executor.close()


async def test_a_claim_whose_submitter_died_before_sbatch_is_taken_over_once_it_expires(
    tmp_path: Path,
) -> None:
    """A waiter makes progress on its own: no attached run, renewal or sweep is needed.

    The first submitter claims the request and dies inside its submission,
    before sbatch. The next run takes the claim over after ``claim_seconds``,
    not after the job timeout.
    """
    world = _World()
    claim_s = 120.0
    dead = world.run(tmp_path / "run-1", "run-1", claim_seconds=claim_s)
    world.scheduler.crash_submits = 1
    await dead.executor.submit(_request(), handle_id="eval-1")
    await dead.parked("eval-1")
    await dead.executor.close()
    assert world.entry_state() is LedgerState.SUBMITTING
    assert world.scheduler.jobs == set()

    start = world.clock.now()
    run = world.run(
        tmp_path / "run-2", "run-2", sleeper=_ClockSleeper(world.clock), claim_seconds=claim_s
    )
    await run.executor.submit(_request(), handle_id="eval-2")
    finished = await _finished(run.executor, "eval-2")

    assert finished.state is EvaluationState.SUCCEEDED
    assert world.scheduler.jobs == {ledger_operation_id(world.only_key(), 1)}
    elapsed = world.clock.now() - start
    assert elapsed <= claim_s + world.scheduler.queue_s + world.scheduler.run_s + 10
    assert elapsed < CONFIG.job_timeout_seconds
    await run.executor.close()
