"""Once a run's Slurm executor has closed, none of the run's jobs is left on the cluster.

Regression: a headless dynamic run ended by SIGTERM left an evaluation job queued on a
shared cluster with no process left to own it. The run used the evaluation ledger
(#2757), whose ``close`` detached every job for a later resume instead of cancelling it,
and an evaluation whose submission was in flight when the shutdown began was submitted
during that shutdown and detached too. A submission that arrives after ``close`` has
begun was also accepted, and its job was never cancelled.

Each example drives one executor through a generated sequence of submissions,
cancellations and pauses with ``close`` at a generated point, on a virtual loop whose
ties are broken by a generated seed, so a shutdown lands before, during and after each
submission's dispatch. Jobs never leave the queue by themselves, so only the executor
can end them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationStep,
    ExecutorRejectedError,
    ExecutorSubmissionError,
    PollPhase,
    is_finished,
)
from vs_sandbox.api.slurm import (
    SharedSlurmAdmission,
    SlurmEvaluationExecutor,
    SlurmLedgerAttachment,
    SlurmStagePayload,
)
from vs_sim.api import yield_now
from vs_sim.api.testing import (
    NO_PROCESSES,
    InlineBlockingRunner,
    ManualClock,
    SimBlockingRunner,
    SimThreads,
    VirtualClock,
    examples,
    run_virtual,
)
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitOutcome,
    ClusterUnknown,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerPolicy,
    SecondsRange,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmJobRequest,
    SlurmSshTransport,
    SlurmTimingProfile,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_slurm.api import (
        ClusterCancelOutcome,
        ClusterInspectOutcome,
        ClusterTarget,
        LedgerEntry,
        LedgerLease,
        LedgerRecord,
        WithdrawalMark,
        WithdrawalUndone,
    )

PROJECT = "demo-0123456789ab"
HANDLES = tuple(f"eval-{index}" for index in range(3))
_BLOCKING = InlineBlockingRunner()
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
    poll_interval_seconds=1.0,
)
# Jobs wait in the queue for a year, far past any run here: only a cancel ends one.
_YEAR_S = 365 * 24 * 3600.0
NEVER = SlurmTimingProfile(
    queue_wait_s=SecondsRange.exactly(_YEAR_S),
    run_s=SecondsRange.exactly(0.0),
    completing_s=SecondsRange.exactly(0.0),
)


class _Scheduler(FakeCluster):
    """The Fake scheduler, noting the operation identity of every submission it receives."""

    def __init__(self, clock: VirtualClock) -> None:
        super().__init__(clock=clock, timing=NEVER)
        self.operations: list[str] = []

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        self.operations.append(operation_id)
        return super().submit(request, operation_id=operation_id)

    def queued(self) -> list[str]:
        """The operations whose job the scheduler still holds, queued or running."""
        live = []
        for operation in dict.fromkeys(self.operations):
            observed = self.inspect(operation)
            if isinstance(observed, ClusterObservation) and not observed.status.terminal:
                live.append(operation)
        return live


@dataclass(frozen=True)
class SubmitEvaluation:
    handle: int


@dataclass(frozen=True)
class Cancel:
    handle: int


@dataclass(frozen=True)
class Pause:
    """Let the executor's own tasks run: ``turns`` loop turns, then ``seconds`` of virtual time."""

    turns: int
    seconds: float


@dataclass(frozen=True)
class Close:
    pass


# Loop turns after the run's steps for a task the executor left behind to reach sbatch.
_SETTLE_TURNS = 20

Step = SubmitEvaluation | Cancel | Pause | Close

_handle = st.integers(min_value=0, max_value=len(HANDLES) - 1)
_steps = st.lists(
    st.one_of(
        st.builds(SubmitEvaluation, _handle),
        st.builds(Cancel, _handle),
        st.builds(
            Pause,
            st.integers(min_value=0, max_value=6),
            st.sampled_from([0.0, 0.5, 3.0]),
        ),
        st.just(Close()),
    ),
    max_size=12,
)

Mode = Literal["direct", "ledger"]


@dataclass
class _Shutdown:
    """One executor over the Fake scheduler, and what the steps saw."""

    root: Path
    mode: Mode
    clock: VirtualClock = field(default_factory=lambda: VirtualClock(1000.0))
    refused: list[str] = field(default_factory=list)
    submitted: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.scheduler = _Scheduler(self.clock)
        workspace = self.root / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "bench.py").write_text("print('bench')\n", encoding="utf-8")
        ledger = None
        if self.mode == "ledger":
            ledger = SlurmLedgerAttachment(
                ledger=FakeEvaluationLedger(project=PROJECT, clock=ManualClock()),
                config=CONFIG,
                project=PROJECT,
                holder="run-1",
                policy=LedgerPolicy(),
                clock=self.clock,
            )
        self.executor = SlurmEvaluationExecutor(
            CONFIG,
            workspace=workspace,
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=self.root / "handles",
            admission=SharedSlurmAdmission(2),
            cluster=self.scheduler,
            clock=self.clock,
            sleeper=self.clock,
            blocking=_BLOCKING,
            processes=NO_PROCESSES,
            ledger=ledger,
        )

    async def play(self, steps: list[Step]) -> None:
        """Start each step in order, pausing where the steps say; return once all have ended.

        Submissions, cancellations and the close run as tasks of their own, so a
        submission can still be dispatching, or only just arriving, while the close runs.
        """
        started: list[asyncio.Task[None]] = []
        for step in steps:
            match step:
                case SubmitEvaluation(handle=index):
                    started.append(asyncio.create_task(self._submit(index)))
                case Cancel(handle=index):
                    started.append(asyncio.create_task(self._cancel(HANDLES[index])))
                case Pause(turns=turns, seconds=seconds):
                    for _ in range(turns):
                        await yield_now()
                    await self.clock.sleep(seconds)
                case Close():
                    started.append(asyncio.create_task(self.executor.close()))
        await asyncio.gather(*started)
        if Close() not in steps:
            await self.executor.close()

    async def queued_after(self, steps: list[Step]) -> list[str]:
        """Play the steps, give any task the executor left behind time to act, read the queue."""
        await self.play(steps)
        for _ in range(_SETTLE_TURNS):
            await yield_now()
        await self.clock.sleep(10 * CONFIG.poll_interval_seconds)
        return self.scheduler.queued()

    async def _submit(self, index: int) -> None:
        handle = HANDLES[index]
        try:
            await self.executor.submit(_request(index), handle_id=handle)
        except (ExecutorRejectedError, ExecutorSubmissionError):
            self.refused.append(handle)
        else:
            self.submitted.add(handle)

    async def _cancel(self, handle: str) -> None:
        # The core cancels only what it has submitted.
        if handle in self.submitted:
            await self.executor.cancel(handle)


def _request(index: int) -> EvaluationRequest:
    return EvaluationRequest(
        key=f"fused-{index}",
        owner_scope="attempt",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(
                    command=f"run-{name} {index}", timeout_seconds=60
                ).model_dump(mode="json"),
            )
            for name in ("accuracy", "benchmark")
        ),
    )


@pytest.mark.regression
@settings(max_examples=examples(pr=40, nightly=400))
@given(
    mode=st.sampled_from(["direct", "ledger"]),
    steps=_steps,
    schedule_seed=st.integers(min_value=0, max_value=2**16),
)
# The reported run: the ledger, a job submitted before the shutdown began.
@example(mode="ledger", steps=[SubmitEvaluation(0), Pause(6, 3.0), Close()], schedule_seed=0)
# A submission that arrives once the executor has closed.
@example(mode="direct", steps=[Close(), SubmitEvaluation(1)], schedule_seed=0)
# Submissions racing the close: the close does not see either of them.
@example(mode="direct", steps=[SubmitEvaluation(0), Close(), SubmitEvaluation(1)], schedule_seed=1)
def test_no_job_of_the_run_is_left_on_the_cluster_once_its_executor_closed(
    tmp_path_factory: pytest.TempPathFactory,
    mode: Mode,
    steps: list[Step],
    schedule_seed: int,
) -> None:
    run = _Shutdown(tmp_path_factory.mktemp("run"), mode)

    # Read the queue on the run's own loop, once every step has returned: the loop's
    # teardown cancels any task the executor left behind, which a process that exits
    # without its executor's help does not do.
    left = run_virtual(run.clock, run.queued_after(steps), schedule_seed=schedule_seed)

    assert left == []


# ---- Two runs on one ledger: shared content, every ending, restarts, cluster faults ----
#
# Each run is an executor with its own handle root and lease holder over one Fake
# ledger and one Fake scheduler. Blocking calls run as simulated threads (``SimThreads``)
# and every ledger and cluster call is a switch point, so the schedule seed interleaves
# one run's attach with another's (or its own sibling's) withdrawal at every step.

# What a request's job does, by its content: queue for a year; complete after a few
# seconds; be refused by the scheduler; end with a stage that timed out.
CONTENTS = ("never_ends", "completes", "rejected", "times_out")
_FINISH_S = 5.0


class _SharedScheduler(FakeCluster):
    """The Fake scheduler, its jobs following their request's content, some calls failing.

    The ``faults``-th inspect and cancel calls (counted together) never reach the
    scheduler, as over a broken connection. Every call is a switch point.
    """

    def __init__(self, clock: VirtualClock, sim: SimThreads, faults: frozenset[int]) -> None:
        super().__init__(clock=clock, timing=NEVER)
        self.sim = sim
        self.faults = faults
        self.calls = 0
        self.contents: dict[str, int] = {}

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        self.sim.sleep(0)
        if isinstance(request, SlurmBatchRequest) and operation_id not in self.contents:
            content = _content_of(request)
            self.contents[operation_id] = content
            name = CONTENTS[content]
            if name == "rejected":
                self.script(operation_id, rejected_reason="QOSMaxSubmitJobPerUserLimit")
            elif name in {"completes", "times_out"}:
                exit_code = 0 if name == "completes" else 124
                self.script(
                    operation_id,
                    queue_wait_s=_FINISH_S,
                    run_s=0.0,
                    completing_s=0.0,
                    result=_batch_result(request, exit_code),
                )
        return super().submit(request, operation_id=operation_id)

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        if self._fails():
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset by peer")
        return super().inspect(target, by_job_id=by_job_id)

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        if self._fails():
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset by peer")
        return super().cancel(target, by_job_id=by_job_id, observed=observed)

    def _fails(self) -> bool:
        self.sim.sleep(0)
        self.calls += 1
        return self.calls in self.faults

    def live(self) -> dict[str, int]:
        """The content of every job the scheduler still holds, by operation, read without faults."""
        reader = self.reopen()
        live = {}
        for operation, content in self.contents.items():
            observed = reader.inspect(operation)
            if isinstance(observed, ClusterObservation) and not observed.status.terminal:
                live[operation] = content
        return live


@dataclass
class _SwitchingLedger:
    """The Fake ledger with every call a switch point of the simulated threads."""

    inner: FakeEvaluationLedger
    sim: SimThreads

    def read(self, key: str) -> LedgerEntry | None:
        self.sim.sleep(0)
        return self.inner.read(key)

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        self.sim.sleep(0)
        return self.inner.claim(record)

    def replace(self, current: LedgerEntry, record: LedgerRecord) -> bool:
        self.sim.sleep(0)
        return self.inner.replace(current, record)

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        self.sim.sleep(0)
        return self.inner.write_withdrawal(current, document)

    def renew(self, key: str, lease: LedgerLease) -> None:
        self.sim.sleep(0)
        self.inner.renew(key, lease)

    def release(self, key: str, holder: str) -> None:
        self.sim.sleep(0)
        self.inner.release(key, holder)

    def list_keys(self) -> tuple[str, ...]:
        self.sim.sleep(0)
        return self.inner.list_keys()

    def operator_file_sha256(self, path: str) -> str | None:
        return self.inner.operator_file_sha256(path)


def _content_of(request: SlurmBatchRequest) -> int:
    (marker,) = (part for part in request.stages[0].command if "content-" in part)
    return int(marker.rsplit("content-", 1)[1])


def _batch_result(request: SlurmBatchRequest, exit_code: int) -> SlurmBatchResult:
    return SlurmBatchResult(
        job_id="0",
        job_exit_code=0,
        job_output="",
        stages=tuple(
            SlurmBatchStageResult(
                name=stage.name,
                exit_code=exit_code,
                stdout="ok",
                stderr="",
                elapsed_seconds=1.0,
                skipped=False,
            )
            for stage in request.stages
        ),
        phase_timings_seconds={},
        content_cache_hits=0,
    )


def _content_request(content: int) -> EvaluationRequest:
    return EvaluationRequest(
        key=f"fused-{content}",
        owner_scope="attempt",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(
                    command=f"run-{name} content-{content}", timeout_seconds=60
                ).model_dump(mode="json"),
            )
            for name in ("accuracy", "benchmark")
        ),
    )


@dataclass(frozen=True)
class RunSubmit:
    run: int
    handle: int


@dataclass(frozen=True)
class RunCancel:
    run: int
    handle: int


@dataclass(frozen=True)
class RunStop:
    """The run stops for good: its executor closes."""

    run: int


@dataclass(frozen=True)
class RunRestart:
    """The run stops and starts again on the same handle root, resuming what it submitted."""

    run: int


RunStep = RunSubmit | RunCancel | Pause | RunStop | RunRestart
_RUNS = 2
_run_index = st.integers(min_value=0, max_value=_RUNS - 1)
_run_steps = st.lists(
    st.one_of(
        st.builds(RunSubmit, _run_index, _handle),
        st.builds(RunCancel, _run_index, _handle),
        st.builds(Pause, st.integers(min_value=0, max_value=6), st.sampled_from([0.0, 1.0, 6.0])),
        st.builds(RunStop, _run_index),
        st.builds(RunRestart, _run_index),
    ),
    max_size=14,
)
# Which content each run's handle asks for: handles of one run, and of both, share some.
_contents = st.lists(
    st.lists(st.integers(min_value=0, max_value=len(CONTENTS) - 1), min_size=3, max_size=3),
    min_size=_RUNS,
    max_size=_RUNS,
)


@dataclass
class _LedgerRun:
    """One run: its executor of the moment, and what the steps asked of it."""

    index: int
    root: Path
    contents: list[int]
    executor: SlurmEvaluationExecutor
    stopped: bool = False
    # Handles accepted by the current executor and never cancelled by the steps.
    wanted: set[str] = field(default_factory=set)
    # Every handle the run ever submitted, which a restart resumes.
    submitted: set[str] = field(default_factory=set)
    # Handles the steps cancelled, and the submissions still in flight.
    cancelled: set[str] = field(default_factory=set)
    submitting: dict[str, asyncio.Task[None]] = field(default_factory=dict)


@dataclass
class _TwoRuns:
    root: Path
    contents: list[list[int]]
    faults: frozenset[int]
    seed: int
    clock: VirtualClock = field(default_factory=lambda: VirtualClock(1000.0))
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.sim = SimThreads(self.clock, schedule_seed=self.seed)
        self.scheduler = _SharedScheduler(self.clock, self.sim, self.faults)
        self.ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
        self.runs = [
            _LedgerRun(index, self.root / f"run-{index}", contents, self._executor(index))
            for index, contents in enumerate(self.contents)
        ]

    def _executor(self, index: int) -> SlurmEvaluationExecutor:
        root = self.root / f"run-{index}"
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "bench.py").write_text("print('bench')\n", encoding="utf-8")
        return SlurmEvaluationExecutor(
            CONFIG,
            workspace=workspace,
            setup_script=None,
            service=None,
            support_trees={},
            handle_root=root / "handles",
            admission=SharedSlurmAdmission(4),
            cluster=self.scheduler,
            clock=self.clock,
            sleeper=self.clock,
            blocking=SimBlockingRunner(self.sim),
            processes=NO_PROCESSES,
            ledger=SlurmLedgerAttachment(
                ledger=_SwitchingLedger(self.ledger.reopen(), self.sim),
                config=CONFIG,
                project=PROJECT,
                holder=f"run-{index}",
                policy=LedgerPolicy(),
                clock=self.clock,
            ),
        )

    async def play(self, steps: list[RunStep]) -> None:
        for step in steps:
            match step:
                case RunSubmit(run=index, handle=handle):
                    run = self.runs[index]
                    task = asyncio.create_task(self._submit(run, handle))
                    run.submitting.setdefault(HANDLES[handle], task)
                    self.tasks.append(task)
                case RunCancel(run=index, handle=handle):
                    run = self.runs[index]
                    run.cancelled.add(HANDLES[handle])
                    run.wanted.discard(HANDLES[handle])
                    self.tasks.append(asyncio.create_task(self._cancel(run, handle)))
                case Pause(turns=turns, seconds=seconds):
                    for _ in range(turns):
                        await yield_now()
                    await self.clock.sleep(seconds)
                case RunStop(run=index):
                    await self._stop(self.runs[index])
                case RunRestart(run=index):
                    await self._restart(self.runs[index])
        await asyncio.gather(*self.tasks)
        await self._settle()

    async def check_and_stop_all(self) -> None:
        """Check what the live runs hold, stop them all, and check nothing is left."""
        live_runs = [run for run in self.runs if not run.stopped]
        wanted = {
            run.contents[int(handle.removeprefix("eval-"))]
            for run in live_runs
            for handle in run.wanted
            if not await _finished(run.executor, handle)
        }
        for operation, content in self.scheduler.live().items():
            assert content in wanted, (
                f"job {operation} ({CONTENTS[content]}) is queued, but no live run wants it"
            )
        for run in live_runs:
            for handle in run.wanted:
                if CONTENTS[run.contents[int(handle.removeprefix("eval-"))]] != "never_ends":
                    continue
                polled = await run.executor.poll(handle)
                # A poll whose own inspect was lost reads unknown: ask again (``faults``
                # holds at most two calls).
                for _ in range(2):
                    if polled.phase is not PollPhase.UNKNOWN:
                        break
                    polled = await run.executor.poll(handle)
                assert polled.phase is PollPhase.QUEUED, (
                    f"run-{run.index} {handle}, never cancelled, reads {polled.phase}"
                )
        for run in live_runs:
            await self._stop(run)
        await self._settle()
        assert self.scheduler.live() == {}, "a job outlived every run"

    async def _submit(self, run: _LedgerRun, handle: int) -> None:
        handle_id = HANDLES[handle]
        executor = run.executor
        try:
            await executor.submit(_content_request(run.contents[handle]), handle_id=handle_id)
        except (ExecutorRejectedError, ExecutorSubmissionError):
            return
        if executor is run.executor and not run.stopped:
            run.submitted.add(handle_id)
            # A handle an earlier executor of the run ended reads that end: the run is done with it.
            if handle_id not in run.cancelled and not await _finished(executor, handle_id):
                run.wanted.add(handle_id)

    async def _cancel(self, run: _LedgerRun, handle: int) -> None:
        handle_id = HANDLES[handle]
        submitting = run.submitting.get(handle_id)
        if submitting is not None:
            await asyncio.shield(submitting)  # the core cancels only what it submitted
        if handle_id not in run.submitted or run.stopped:
            # Nothing to cancel: a later submission of the handle is wanted afresh.
            run.cancelled.discard(handle_id)
            return
        await run.executor.cancel(handle_id)

    async def _stop(self, run: _LedgerRun) -> None:
        if run.stopped:
            return
        run.stopped = True
        run.wanted.clear()
        await run.executor.close()

    async def _restart(self, run: _LedgerRun) -> None:
        if run.stopped:
            return
        await self._stop(run)
        run.executor = self._executor(run.index)
        run.stopped = False
        run.submitting.clear()
        for handle_id in sorted(run.submitted):
            await run.executor.inspect(handle_id)
            if not await _finished(run.executor, handle_id):
                run.wanted.add(handle_id)  # resumed: the run still wants its result

    async def _settle(self) -> None:
        for _ in range(_SETTLE_TURNS):
            await yield_now()
        await self.clock.sleep(4 * _FINISH_S)


async def _finished(executor: SlurmEvaluationExecutor, handle_id: str) -> bool:
    observed = await executor.inspect_only(handle_id)
    return observed is not None and is_finished(observed.state)


@settings(max_examples=examples(pr=40, nightly=400), deadline=None)
@given(
    contents=_contents,
    steps=_run_steps,
    faults=st.frozensets(st.integers(min_value=1, max_value=60), max_size=2),
    schedule_seed=st.integers(min_value=0, max_value=2**16),
)
# Two evaluations of one content in one run; one is cancelled, the other still needs the job.
@example(
    contents=[[0, 0, 1], [0, 1, 2]],
    steps=[RunSubmit(0, 0), RunSubmit(0, 1), Pause(6, 1.0), RunCancel(0, 0)],
    faults=frozenset(),
    schedule_seed=0,
)
# A rejected evaluation, then another of the same content, then a stop.
@example(
    contents=[[2, 2, 0], [0, 1, 2]],
    steps=[RunSubmit(0, 0), Pause(6, 6.0), RunSubmit(0, 1), Pause(6, 1.0), RunStop(0)],
    faults=frozenset(),
    schedule_seed=0,
)
# Both runs want one job; one restarts, cancels its evaluation, and stops.
@example(
    contents=[[0, 1, 2], [0, 1, 2]],
    steps=[
        RunSubmit(0, 0),
        RunSubmit(1, 0),
        Pause(6, 1.0),
        RunRestart(0),
        RunCancel(0, 0),
        RunStop(0),
    ],
    faults=frozenset(),
    schedule_seed=0,
)
# The check's own poll of a queued job loses its inspect.
@example(
    contents=[[0, 0, 0], [0, 0, 0]],
    steps=[RunSubmit(0, 0)],
    faults=frozenset([21]),
    schedule_seed=0,
)
# A cancel of a handle never submitted, then its submission: the run wants that job.
@example(
    contents=[[0, 0, 0], [0, 0, 0]],
    steps=[RunSubmit(0, 0), RunCancel(1, 2), RunStop(0), Pause(0, 0.0), RunSubmit(1, 2)],
    faults=frozenset(),
    schedule_seed=1,
)
def test_a_stopped_run_leaves_no_job_only_it_wanted_and_takes_none_a_live_run_needs(
    tmp_path_factory: pytest.TempPathFactory,
    contents: list[list[int]],
    steps: list[RunStep],
    faults: frozenset[int],
    schedule_seed: int,
) -> None:
    world = _TwoRuns(tmp_path_factory.mktemp("runs"), contents, faults, schedule_seed)

    async def main() -> None:
        await world.play(steps)
        await world.check_and_stop_all()

    run_virtual(world.clock, main(), schedule_seed=schedule_seed, driver=world.sim)


def test_a_run_never_leases_a_request_it_is_withdrawing(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A sibling evaluation that asks for a request while the run withdraws it waits for the end.

    The first evaluation's cancel withdraws the request; its scancel takes ten
    seconds. A second evaluation of the same content asks for it meanwhile. It
    must not lease and attach to the attempt being cancelled (it would wait on a
    cancelled job): it attaches once the withdrawal ended, to a new attempt.
    """
    clock = VirtualClock(1000.0)
    sim = SimThreads(clock, schedule_seed=None)
    held = {"armed": False}

    class _SlowCancel(_SharedScheduler):
        def cancel(
            self,
            target: ClusterTarget,
            *,
            by_job_id: bool = False,
            observed: ClusterObservation | None = None,
        ) -> ClusterCancelOutcome:
            if held["armed"]:
                held["armed"] = False
                self.sim.sleep(10.0)
            return super().cancel(target, by_job_id=by_job_id, observed=observed)

    renewals: list[tuple[float, str]] = []
    withdrawing: list[tuple[float, float]] = []

    @dataclass
    class _Watched(_SwitchingLedger):
        def renew(self, key: str, lease: LedgerLease) -> None:
            renewals.append((clock.now(), key))
            super().renew(key, lease)

    scheduler = _SlowCancel(clock, sim, frozenset())
    ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
    root = tmp_path_factory.mktemp("run")
    (root / "workspace").mkdir()
    (root / "workspace" / "bench.py").write_text("print('bench')\n", encoding="utf-8")
    executor = SlurmEvaluationExecutor(
        CONFIG,
        workspace=root / "workspace",
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        admission=SharedSlurmAdmission(4),
        cluster=scheduler,
        clock=clock,
        sleeper=clock,
        blocking=SimBlockingRunner(sim),
        processes=NO_PROCESSES,
        ledger=SlurmLedgerAttachment(
            ledger=_Watched(ledger, sim),
            config=CONFIG,
            project=PROJECT,
            holder="run-0",
            policy=LedgerPolicy(),
            clock=clock,
        ),
    )

    async def main() -> None:
        await executor.submit(_content_request(0), handle_id="eval-0")
        await clock.sleep(3.0)
        (first,) = scheduler.live()
        held["armed"] = True
        started = clock.now()
        cancelling = asyncio.create_task(executor.cancel("eval-0"))
        await clock.sleep(1.0)
        await executor.submit(_content_request(0), handle_id="eval-1")
        await cancelling
        withdrawing.append((started, clock.now()))
        await clock.sleep(3.0)
        assert first not in scheduler.live()
        polled = await executor.poll("eval-1")
        assert polled.phase is PollPhase.QUEUED
        assert len(scheduler.live()) == 1
        await executor.close()

    run_virtual(clock, main(), driver=sim)
    ((start, end),) = withdrawing
    during = [at for at, _ in renewals if start < at < end]
    assert during == [], f"the run leased the request it was withdrawing, at {during}"
