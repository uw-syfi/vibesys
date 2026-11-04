"""The Slurm executor with the evaluation ledger (#2757): runs share jobs, and stopped runs leave none.

A run that stops, or cancels an evaluation, withdraws each request no other run
wants, cancelling its job. With ``keep_jobs_on_stop`` it cancels nothing: a
restart reattaches to the same job, and the grace sweep ends one nobody comes
back for. A crash leaves jobs as that policy does; the tests that stand for a
crash close a run under it.

Each test runs one or two "runs" (executors with their own run directory and
lease holder) against one Fake scheduler and one Fake ledger. The scheduler's
jobs follow its clock, which only the test advances. Each run's wait loop parks
in its first poll sleep, so the test drives every later step through ``poll``,
the read the core makes, and nothing happens behind its back.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    PollPhase,
    is_finished,
)
from vs_evaluation.api.testing import wait_until_executor_started
from vs_sandbox.api.slurm import (
    CLOSE_RELEASE_BOUND_S,
    AttachPending,
    SharedSlurmAdmission,
    SlurmEvaluationExecutor,
    SlurmLedgerAttachment,
    SlurmStagePayload,
)
from vs_sim.api import ThreadBlockingRunner, yield_now
from vs_sim.api.testing import NO_PROCESSES, ManualClock, yield_until
from vs_slurm.api import (
    ClusterCancelOutcome,
    ClusterCollectOutcome,
    ClusterObservation,
    ClusterSubmitOutcome,
    ClusterTarget,
    ClusterUnknown,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerLabels,
    LedgerPolicy,
    LedgerState,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmJobRequest,
    SlurmJobStatus,
    SlurmSshTransport,
    WithdrawalTiming,
    Withdrawer,
    Withdrawn,
    ledger_operation_id,
    withdraw,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.asyncio

PROJECT = "demo-0123456789ab"
LEASE_S = 300.0
GRACE_S = 1800.0
DEFAULT_POLICY = LedgerPolicy(lease_seconds=LEASE_S, grace_seconds=GRACE_S)
# A run that stops keeping its jobs, for a later restart, leaves them as a crash does.
KEEPING = DEFAULT_POLICY.model_copy(update={"keep_jobs_on_stop": True})
# Only guards a hang: every wait below ends on an event the test already caused.
_BOUND_S = 60.0
_BLOCKING = ThreadBlockingRunner()
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
    poll_interval_seconds=1.0,
)


def _is_poll_pause(seconds: float) -> bool:
    """Whether a sleep is a wait loop's pause between polls, not the lease keeper's period."""
    return seconds <= CONFIG.poll_interval_seconds


class _ParkingSleeper:
    """The first sleep of a run's wait loop parks until the run closes.

    The lease keeper's longer sleeps park too, without counting: no renewal
    happens behind the test's back.
    """

    def __init__(self) -> None:
        self.parked = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        if _is_poll_pause(seconds):
            self.parked.set()
        await asyncio.Event().wait()


_DIED = "the submitting process died before sbatch"


def _die() -> None:
    raise OSError(_DIED)


class _Scheduler(FakeCluster):
    """A scheduler whose every batch queues, runs and succeeds on its clock; it records each job."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.manual = clock
        self.jobs: set[str] = set()
        self.queue_s = 100.0
        self.run_s = 100.0
        # How many submissions die before sbatch, as a submitter process that crashed would.
        self.crash_submits = 0
        # Each stage's exit code; None leaves the stage no result file.
        self.stage_exit: int | None = 0
        # Runs inside the next batch submission, before sbatch: a slow staging step.
        self.on_staging: Callable[[], None] | None = None
        # The last batch request submitted, as another run of the same content asks for it.
        self.staged: SlurmBatchRequest | None = None
        # How many collections read nothing, as when the connection drops mid-download.
        self.dropped_collections = 0
        self.collections = 0
        # How many submissions the scheduler refuses, and how many lose their answer.
        self.reject_submits = 0
        self.lose_submit_replies = 0
        # How many cancels never reach the scheduler, as over a broken connection.
        self.failing_cancels = 0
        # How long each cancel takes on the clock, as on a sick controller; and how many came.
        self.cancel_s = 0.0
        self.cancels = 0

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        if self.crash_submits:
            # The submitter stages the job and dies before sbatch: the scheduler side
            # holds a half-done submission under the name, and no job.
            self.crash_submits -= 1
            self.script(operation_id, on_dispatch=_die)
            return super().submit(request, operation_id=operation_id)
        if isinstance(request, SlurmBatchRequest) and self.reject_submits:
            self.reject_submits -= 1
            self.staged = request
            self.script(operation_id, rejected_reason="QOSMaxSubmitJobPerUserLimit")
            return super().submit(request, operation_id=operation_id)
        if isinstance(request, SlurmBatchRequest) and operation_id not in self.jobs:
            self.jobs.add(operation_id)
            self.staged = request
            staging, self.on_staging = self.on_staging, None
            lost = self.lose_submit_replies > 0
            self.lose_submit_replies -= int(lost)
            self.script(
                operation_id,
                lost_submit_reply=lost,
                on_dispatch=staging or (lambda: None),
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
                            exit_code=self.stage_exit,
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

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCollectOutcome:
        self.collections += 1
        if self.dropped_collections:
            self.dropped_collections -= 1
            return ClusterUnknown(
                operation_id=target if isinstance(target, str) else None,
                reason="download of the results did not complete: connection reset by peer",
            )
        return super().collect(target, by_job_id=by_job_id, observed=observed)

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        self.cancels += 1
        self.manual.advance(self.cancel_s)
        if self.failing_cancels:
            self.failing_cancels -= 1
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset by peer")
        return super().cancel(target, by_job_id=by_job_id, observed=observed)

    def status(self, operation_id: str) -> SlurmJobStatus:
        observed = self.inspect(operation_id)
        assert isinstance(observed, ClusterObservation)
        return observed.status


class _ClockSleeper:
    """Every poll pause moves the shared clock on by its length: time passes only by waiting."""

    def __init__(self, clock: ManualClock) -> None:
        self.parked = asyncio.Event()
        self._clock = clock

    async def sleep(self, seconds: float) -> None:
        if not _is_poll_pause(seconds):
            await asyncio.Event().wait()  # the lease keeper's period: never ends here
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
        polled = await yield_until(
            lambda: self.executor.poll(handle_id),
            lambda seen: seen.phase is PollPhase.ENDED,
            "the live Slurm owner to settle its terminal observation",
        )
        assert polled.terminal is not None
        return polled.terminal


@dataclass
class _World:
    clock: ManualClock = field(default_factory=lambda: ManualClock(1000.0))
    ledger: FakeEvaluationLedger = field(
        default_factory=lambda: FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
    )
    # How long a cancel confirms its job ended; the executor's own default unless set.
    cancel_confirmation_seconds: float = 120.0
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
        policy: LedgerPolicy | None = DEFAULT_POLICY,
    ) -> _Run:
        """A run of this world's executor; ``policy`` None: a run without the ledger."""
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
            # Room for every evaluation a test runs at once: none waits on another.
            admission=SharedSlurmAdmission(4),
            cluster=self.scheduler,
            clock=self.clock,
            sleeper=sleeper,
            blocking=_BLOCKING,
            processes=NO_PROCESSES,
            ledger=SlurmLedgerAttachment(
                ledger=self.ledger.reopen(),
                config=CONFIG,
                project=PROJECT,
                holder=holder,
                policy=policy,
                clock=self.clock,
            )
            if policy is not None
            else None,
            ledger_candidate="0f3c9e1",
            cancel_confirmation_seconds=self.cancel_confirmation_seconds,
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
    first = world.run(tmp_path / "run-1", "run-1", policy=KEEPING)
    await _attached(first, "eval-1")
    (operation,) = world.scheduler.jobs

    await first.executor.close()
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING
    assert world.entry_state() is LedgerState.SUBMITTED

    if restart == "resume":
        second = world.run(tmp_path / "run-1", "run-1", policy=KEEPING)
        handle_id = "eval-1"
        assert await second.executor.inspect(handle_id) is not None
    else:
        # A fresh relaunch: a new run directory, a new worktree path, the same content.
        second = world.run(tmp_path / "run-2" / "elsewhere", "run-2", policy=KEEPING)
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
    first = world.run(tmp_path / "run-1", "run-1", policy=KEEPING)
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
    # Each run collected the shared job, then stopped using the request it completed.
    assert entry.record.state is LedgerState.COMPLETED
    assert entry.leases == ()


@pytest.mark.parametrize("policy", [DEFAULT_POLICY, KEEPING], ids=["default", "keeping"])
async def test_cancel_lets_go_of_the_job_and_ends_the_evaluation_for_this_run(
    tmp_path: Path, policy: LedgerPolicy
) -> None:
    keep_jobs_on_stop = policy.keep_jobs_on_stop
    world = _World()
    run = world.run(tmp_path / "run-1", "run-1", policy=policy)
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs

    await run.executor.cancel("eval-1")

    # No other run wants the request: by default its job is cancelled with it.
    expected = SlurmJobStatus.PENDING if keep_jobs_on_stop else SlurmJobStatus.CANCELLED
    assert world.scheduler.status(operation) is expected
    entry = world.ledger.read(world.only_key())
    assert entry is not None
    assert entry.leases == ()
    assert (entry.record.state is LedgerState.WITHDRAWN) is not keep_jobs_on_stop
    await run.executor.close()
    restarted = world.run(tmp_path / "run-1", "run-1", policy=policy)
    assert (await restarted.terminal("eval-1")).state is EvaluationState.CANCELED
    await restarted.executor.close()


async def test_a_stop_cancels_the_job_of_a_request_no_other_run_wants(tmp_path: Path) -> None:
    world = _World()
    run = world.run(tmp_path / "run-1", "run-1")
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs

    await run.executor.close()

    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED
    assert world.entry_state() is LedgerState.WITHDRAWN


async def test_a_job_another_run_still_wants_outlives_a_stop_until_that_run_stops(
    tmp_path: Path,
) -> None:
    world = _World()
    runs = [world.run(tmp_path / f"run-{index}", f"run-{index}") for index in range(2)]
    for index, run in enumerate(runs):
        await _attached(run, f"eval-{index}")
    (operation,) = world.scheduler.jobs

    await runs[0].executor.close()
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING
    assert world.entry_state() is LedgerState.SUBMITTED

    await runs[1].executor.close()
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED


def _submitted_attempt(world: _World) -> int | None:
    """The attempt of the request whose job the ledger names, if it names one."""
    entry = world.ledger.read(world.only_key())
    if entry is None or entry.record.state is not LedgerState.SUBMITTED:
        return None
    return entry.record.attempt


async def test_an_attached_run_submits_again_a_request_withdrawn_without_its_asking(
    tmp_path: Path,
) -> None:
    """An operator's removal, or a sweep after this run's lease lapsed, is not this run's cancel.

    The evaluation still wants its result: it claims a new attempt and waits on
    its job, and a stop cancels that job.
    """
    world = _World()
    run = world.run(tmp_path / "run-1", "run-1")
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs

    outcome = withdraw(
        world.ledger,
        world.scheduler,
        world.only_key(),
        Withdrawer(
            by="vibesys-evals",
            reason="removed",
            timing=WithdrawalTiming(
                clock=world.clock, call_seconds=CONFIG.transport_timeout_seconds
            ),
        ),
    )

    assert isinstance(outcome, Withdrawn)
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED
    # The run reads the ledger again a third of a lease period after its last renewal.
    world.clock.advance(LEASE_S / 3)
    polled = await yield_until(
        lambda: run.executor.poll("eval-1"),
        lambda seen: _submitted_attempt(world) == 1 or seen.phase is PollPhase.ENDED,
        "the evaluation to submit its request again",
    )
    assert polled.phase is not PollPhase.ENDED
    (again,) = world.scheduler.jobs - {operation}
    assert again == ledger_operation_id(world.only_key(), 1)
    assert world.scheduler.status(again) is SlurmJobStatus.PENDING
    await run.executor.close()
    assert world.scheduler.status(again) is SlurmJobStatus.CANCELLED


async def test_an_attached_run_submits_again_a_request_another_runs_sweep_withdrew(
    tmp_path: Path,
) -> None:
    """r3173 LOW-6: a sweep after this run's lease lapsed is not this run's cancel either.

    The test above withdraws through ``withdraw``, whose signature the fix
    changed, so at the reviewed code it failed on that call, not on the bug.
    This one reaches the withdrawal only through the executor's ``submit`` and
    ``poll``, whose signatures predate the fix: another run's poll sweeps the
    request once this run's lease lapsed past the grace period. The evaluation
    still wants its result, so it claims a new attempt and waits on its job.
    """
    world = _World()
    world.scheduler.queue_s = math.inf
    run = world.run(tmp_path / "run-1", "run-1")
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs
    other = world.run(tmp_path / "run-2", "run-2", content="print('other')")
    await _attached(other, "eval-2")
    (key,) = (k for k in world.ledger.list_keys() if ledger_operation_id(k, 0) == operation)

    # Run-1 does not poll for longer than its lease and the grace period (its
    # event loop stalled); run-2 polls, renewing its own lease first, and sweeps.
    world.clock.advance(LEASE_S + GRACE_S + 1)
    assert (await other.executor.poll("eval-2")).phase is PollPhase.QUEUED
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED

    def resubmitted() -> bool:
        entry = world.ledger.read(key)
        return entry is not None and entry.record.attempt == 1 and entry.record.job is not None

    polled = await yield_until(
        lambda: run.executor.poll("eval-1"),
        lambda seen: resubmitted() or seen.phase is PollPhase.ENDED,
        "the evaluation to submit its request again",
    )
    assert polled.phase is not PollPhase.ENDED
    again = ledger_operation_id(key, 1)
    assert world.scheduler.status(again) is SlurmJobStatus.PENDING
    await run.executor.close()
    assert world.scheduler.status(again) is SlurmJobStatus.CANCELLED
    await other.executor.close()


async def test_a_job_no_run_leases_is_cancelled_by_another_runs_sweep_after_its_grace(
    tmp_path: Path,
) -> None:
    world = _World()
    world.scheduler.queue_s = math.inf
    # A run that stops keeping its job, as a crashed one leaves it.
    stopped = world.run(tmp_path / "run-1", "run-1", policy=KEEPING)
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
    # A crash leaves the claim behind, as a stop that keeps its jobs does.
    dead = world.run(
        tmp_path / "run-1",
        "run-1",
        policy=KEEPING.model_copy(update={"claim_seconds": claim_s}),
    )
    world.scheduler.crash_submits = 1
    await dead.executor.submit(_request(), handle_id="eval-1")
    await dead.parked("eval-1")
    await dead.executor.close()
    assert world.entry_state() is LedgerState.SUBMITTING
    assert world.scheduler.jobs == set()

    start = world.clock.now()
    run = world.run(
        tmp_path / "run-2",
        "run-2",
        sleeper=_ClockSleeper(world.clock),
        policy=DEFAULT_POLICY.model_copy(update={"claim_seconds": claim_s}),
    )
    await run.executor.submit(_request(), handle_id="eval-2")
    finished = await _finished(run.executor, "eval-2")

    assert finished.state is EvaluationState.SUCCEEDED
    assert world.scheduler.jobs == {ledger_operation_id(world.only_key(), 1)}
    elapsed = world.clock.now() - start
    assert elapsed <= claim_s + world.scheduler.queue_s + world.scheduler.run_s + 10
    assert elapsed < CONFIG.job_timeout_seconds
    await run.executor.close()


@pytest.mark.parametrize(
    ("stage_exit", "reused"),
    [(0, True), (1, True), (124, False), (None, False)],
    ids=["passed", "failed_stage", "timed_out", "uncollectable"],
)
async def test_a_finished_job_is_reused_only_if_every_stage_left_its_own_result(
    tmp_path: Path, stage_exit: int | None, *, reused: bool
) -> None:
    """The run settles its attempt from what it collected, never from Slurm's COMPLETED alone."""
    world = _World()
    world.scheduler.stage_exit = stage_exit
    first = world.run(tmp_path / "run-1", "run-1")
    await _attached(first, "eval-1")
    world.clock.advance(world.scheduler.queue_s + world.scheduler.run_s)
    expected = EvaluationState.SUCCEEDED if stage_exit == 0 else EvaluationState.FAILED
    assert (await first.terminal("eval-1")).state is expected
    await first.executor.close()
    assert world.entry_state() is (LedgerState.COMPLETED if reused else LedgerState.FAILED)

    second = world.run(tmp_path / "run-2", "run-2")
    await second.executor.submit(_request(), handle_id="eval-2")
    if reused:
        assert (await _finished(second.executor, "eval-2")).state is expected
        assert len(world.scheduler.jobs) == 1
    else:
        await second.parked("eval-2")
        assert world.scheduler.jobs == {
            ledger_operation_id(world.only_key(), 0),
            ledger_operation_id(world.only_key(), 1),
        }
    await second.executor.close()


@pytest.mark.parametrize("drops", ["one", "every"])
async def test_a_collection_that_read_nothing_is_tried_again_and_never_settles_or_scores(
    tmp_path: Path, drops: str
) -> None:
    """A finished job whose download was cut short is collected again, never run again.

    One cut collection is tried again a poll interval later within the same run.
    When every try of a run is cut, that run's evaluation ends with an unknown
    outcome and no stage result, and its ledger attempt stays submitted: the next
    ask collects the same job instead of running it for another half hour.
    """
    world = _World()
    world.scheduler.dropped_collections = 1 if drops == "one" else 10**6
    first = world.run(tmp_path / "run-1", "run-1", sleeper=_ClockSleeper(world.clock))
    await first.executor.submit(_request(), handle_id="eval-1")
    ended = await _finished(first.executor, "eval-1")
    await first.executor.close()

    if drops == "one":
        assert ended.state is EvaluationState.SUCCEEDED
        assert world.scheduler.collections == 2
        assert world.entry_state() is LedgerState.COMPLETED
        return
    assert ended.state is EvaluationState.FAILED
    assert ended.stage_results == ()
    assert ended.failure is not None
    assert "unknown outcome" in ended.failure
    assert world.entry_state() is LedgerState.SUBMITTED

    world.scheduler.dropped_collections = 0
    second = world.run(tmp_path / "run-2", "run-2", sleeper=_ClockSleeper(world.clock))
    await second.executor.submit(_request(), handle_id="eval-2")
    assert (await _finished(second.executor, "eval-2")).state is EvaluationState.SUCCEEDED
    assert len(world.scheduler.jobs) == 1
    assert world.entry_state() is LedgerState.COMPLETED
    await second.executor.close()


class _SteppedKeeperSleeper(_ParkingSleeper):
    """Poll pauses park; each lease-keeper period ends only when the test steps it."""

    def __init__(self) -> None:
        super().__init__()
        self.keeper_waiting = asyncio.Event()
        self._step = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        if _is_poll_pause(seconds):
            await super().sleep(seconds)
            return
        self.keeper_waiting.set()
        await self._step.wait()
        self._step.clear()

    async def step(self) -> None:
        """End the keeper's current period and wait until it renewed and sleeps again."""
        self.keeper_waiting.clear()
        self._step.set()
        await asyncio.wait_for(self.keeper_waiting.wait(), _BOUND_S)


async def test_a_slow_submission_keeps_its_claim_by_renewing_it(tmp_path: Path) -> None:
    """Staging outlasts the claim window; the run's lease keeper renews the claim meanwhile,
    so another run asking for the request waits instead of taking it over."""
    world = _World()
    claim_s = 120.0
    sleeper = _SteppedKeeperSleeper()
    run = world.run(
        tmp_path / "run-1",
        "run-1",
        sleeper=sleeper,
        policy=DEFAULT_POLICY.model_copy(update={"claim_seconds": claim_s}),
    )
    loop = asyncio.get_running_loop()
    waited: list[object] = []

    async def stage_for_three_lease_thirds() -> None:
        await asyncio.wait_for(sleeper.keeper_waiting.wait(), _BOUND_S)
        for _ in range(3):
            world.clock.advance(LEASE_S / 3)
            await sleeper.step()
        assert world.clock.now() - 1000.0 > claim_s  # the claim's first window ran out
        ledger = SlurmLedgerAttachment(
            ledger=world.ledger.reopen(),
            config=CONFIG,
            project=PROJECT,
            holder="run-2",
            policy=LedgerPolicy(lease_seconds=LEASE_S, claim_seconds=claim_s),
            clock=world.clock,
        )
        batch = world.scheduler.staged
        assert batch is not None
        waited.append(ledger.attach(world.scheduler, batch, labels=LedgerLabels()))

    def staging() -> None:
        # Runs on the submitting worker thread, inside the submission, before sbatch.
        asyncio.run_coroutine_threadsafe(stage_for_three_lease_thirds(), loop).result(
            timeout=_BOUND_S
        )

    world.scheduler.on_staging = staging
    await _attached(run, "eval-1")

    assert len(waited) == 1
    assert isinstance(waited[0], AttachPending)
    (operation,) = world.scheduler.jobs
    assert operation == ledger_operation_id(world.only_key(), 0)
    assert world.entry_state() is LedgerState.SUBMITTED
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING
    await run.executor.close()


async def _queued(run: _Run, handle_id: str) -> None:
    """Return once the evaluation's job is queued on the cluster, as the core's poll reads it.

    A poll before the job exists reads QUEUED too, with a detail saying why
    (awaiting admission); one of a queued job says nothing more.
    """
    await yield_until(
        lambda: run.executor.poll(handle_id),
        lambda seen: seen.phase is PollPhase.QUEUED and not seen.detail,
        "the evaluation's job to queue",
    )


@pytest.mark.parametrize("ending", ["rejected", "timed_out", "succeeded"])
async def test_an_evaluation_that_ended_stops_using_its_request(
    tmp_path: Path, ending: str
) -> None:
    """However an evaluation ends, it no longer counts as a user of its request.

    The run drops its lease once no evaluation of it uses the request. A later
    evaluation of the same content that needs a new job gets one, and a stop
    cancels that job: a use left behind by the ended evaluation kept it queued.
    """
    world = _World()
    world.scheduler.reject_submits = int(ending == "rejected")
    world.scheduler.stage_exit = 124 if ending == "timed_out" else 0
    run = world.run(tmp_path / "run-1", "run-1", sleeper=_ClockSleeper(world.clock))
    await run.executor.submit(_request(), handle_id="eval-1")
    ended = await _finished(run.executor, "eval-1")
    expected = EvaluationState.SUCCEEDED if ending == "succeeded" else EvaluationState.FAILED
    assert ended.state is expected
    entry = world.ledger.read(world.only_key())
    assert entry is not None
    assert entry.leases == ()
    if ending == "succeeded":
        await run.executor.close()
        return

    earlier = set(world.scheduler.jobs)
    world.scheduler.queue_s = math.inf
    await run.executor.submit(_request(), handle_id="eval-2")
    await _queued(run, "eval-2")
    (operation,) = world.scheduler.jobs - earlier
    await run.executor.close()
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED


async def test_cancelling_one_of_two_resumed_evaluations_of_one_content_keeps_their_job(
    tmp_path: Path,
) -> None:
    """A restarted run counts every evaluation it resumes as a user of its request."""
    world = _World()
    first = world.run(tmp_path / "run-1", "run-1", policy=KEEPING)
    await _attached(first, "eval-1")
    first.sleeper.parked.clear()
    await _attached(first, "eval-2")
    (operation,) = world.scheduler.jobs
    await first.executor.close()  # stands for a crash: the job stays
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING

    second = world.run(tmp_path / "run-1", "run-1")
    for handle_id in ("eval-1", "eval-2"):
        assert await second.executor.inspect(handle_id) is not None
    await second.parked("eval-2")
    await second.executor.cancel("eval-1")
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING, world.entry_state()

    await second.executor.close()
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED


async def test_a_cancel_whose_withdrawal_ended_unknown_withdraws_it_when_tried_again(
    tmp_path: Path,
) -> None:
    """The run keeps the request of a cancel it could not finish, so a retry finishes it.

    The submission's answer was lost, so the evaluation waits to find its job by
    name and has none recorded; the cluster then drops every scancel. A cancel
    that could not withdraw the request reports that, and a later cancel, with
    the cluster reachable again, cancels the job instead of forgetting it.
    """
    world = _World()
    world.scheduler.lose_submit_replies = 1
    run = world.run(tmp_path / "run-1", "run-1")
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING

    world.scheduler.failing_cancels = 10**6
    with contextlib.suppress(ExecutorCancellationUnknownError):
        await run.executor.cancel("eval-1")
    world.scheduler.failing_cancels = 0
    await run.executor.cancel("eval-1")

    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED
    assert world.entry_state() is LedgerState.WITHDRAWN
    await run.executor.close()


def _tries_within_bound(cancel_s: float, lost: int) -> tuple[int, bool]:
    """How many relinquish tries a close makes, and whether one cancelled the job.

    Each case here ends within the relinquish's own retries; only the bound stops it.
    """
    tries = 0
    while tries * cancel_s < CLOSE_RELEASE_BOUND_S:
        tries += 1
        if tries > lost:
            return tries, True
    return tries, False


@pytest.mark.regression
@pytest.mark.parametrize(
    ("cancel_s", "lost"),
    [(CONFIG.transport_timeout_seconds, 10**6), (20.0, 10**6), (5.0, 1), (5.0, 0)],
)
async def test_a_close_on_a_sick_controller_gives_up_releasing_and_detaches(
    tmp_path: Path, cancel_s: float, lost: int
) -> None:
    """periodic-1330 LOW-6: a SIGTERM's close relinquished every job, however long it took.

    Each scancel takes ``cancel_s`` and the first ``lost`` are lost, so a relinquish ends
    unknown and is tried again. A close starts no try once ``CLOSE_RELEASE_BOUND_S`` has
    passed: it detaches what is left, as a run that keeps its jobs on a stop does, so the
    job's record stays resumable and a later run cancels it. A try that is answered within
    the bound cancels the job.
    """
    world = _World()
    world.scheduler.queue_s = math.inf
    run = world.run(tmp_path / "run-1", "run-1")
    await _attached(run, "eval-1")
    (operation,) = world.scheduler.jobs
    world.scheduler.failing_cancels = lost
    world.scheduler.cancel_s = cancel_s
    tries, cancelled = _tries_within_bound(cancel_s, lost)

    await asyncio.wait_for(run.executor.close(), timeout=_BOUND_S)

    if cancelled:
        assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED
        return
    assert world.scheduler.cancels == tries
    assert world.scheduler.status(operation) is SlurmJobStatus.PENDING
    world.scheduler.failing_cancels = 0
    restarted = world.run(tmp_path / "run-1", "run-1")
    assert await restarted.executor.inspect("eval-1") is not None
    await restarted.executor.cancel("eval-1")
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED
    await restarted.executor.close()


@pytest.mark.regression
async def test_a_close_past_its_bound_still_cancels_a_job_from_before_the_ledger(
    tmp_path: Path,
) -> None:
    """PR #3222 LOW-1: past the bound, a close detached a job that had no ledger entry.

    The run submitted its job without the ledger and was resumed with it, so the job has
    no ledger key and no ledger sweep will ever withdraw it: only this close can cancel
    it. Its first scancel is lost and takes the whole bound; the close's next try must
    still cancel the job, since giving up applies only to ledger releases.
    """
    # One confirmation inspect per cancel: the close never pauses between them.
    world = _World(cancel_confirmation_seconds=CONFIG.poll_interval_seconds)
    world.scheduler.queue_s = math.inf
    without = world.run(tmp_path / "run-1", "run-1", policy=None)
    await _attached(without, "eval-1")
    (operation,) = world.scheduler.jobs
    assert not world.ledger.list_keys()

    resumed = world.run(tmp_path / "run-1", "run-1")
    assert await resumed.executor.inspect("eval-1") is not None
    world.scheduler.failing_cancels = 1
    world.scheduler.cancel_s = CLOSE_RELEASE_BOUND_S

    await asyncio.wait_for(resumed.executor.close(), timeout=_BOUND_S)

    assert world.scheduler.cancels == 2
    assert world.scheduler.status(operation) is SlurmJobStatus.CANCELLED
    await without.executor.close()
