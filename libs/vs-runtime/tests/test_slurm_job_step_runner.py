"""SlurmJobStepRunner passes the StepRunner contract over the Fake Slurm cluster and ledger.

The runner sits on the production executor stack (semantic Slurm executor, the sandbox's Slurm
executor, an evaluation ledger attachment) with only the cluster and the ledger faked, so
"submits once" is counted on the cluster, and a restarted process is a second stack over the
same cluster, ledger and state.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from vs_core.api import (
    AttemptId,
    AttemptRef,
    CloseAttemptScope,
    CollectEvidence,
    HostFence,
    HostId,
    MeasurementFailure,
    ObservationStatus,
    ObserveOwnedJob,
    RequestId,
    ResourceId,
)
from vs_core.api.recovery import Cancelled, CancelReason, Finished, interruption_of
from vs_runtime.api.core import (
    JobBooks,
    JobRegistry,
    KeyedDocuments,
    MeasurementRequests,
    ReceiptEvidenceLedger,
    RequestExecutors,
)
from vs_runtime.api.evaluation_step_runners import SlurmJobStepRunner, SlurmScheduler
from vs_runtime.api.executor_contexts import RevocableLease, receipt_store_in
from vs_runtime.api.executor_step_runner import DECLARED_REPEATS, ExecutorStepRunner
from vs_runtime.api.slurm_evaluation_stack import (
    ScenarioCluster,
    Stack,
    build_stack,
    plan,
    submission,
)
from vs_runtime.api.step_runner import Adopted, CancelSignal, Cleared, ClearUnavailableError
from vs_runtime.api.testing import StepRunnerContract, StepRunnerUnderTest
from vs_sandbox.api.slurm import SlurmLedgerAttachment
from vs_sim.api import SeededRandom, yield_now
from vs_sim.api.testing import (
    DetachedBlockingRunner,
    ManualClock,
    VirtualClock,
    examples,
    run_virtual,
    yield_until,
)
from vs_slurm.api import (
    ClusterInspectOutcome,
    ClusterUnknown,
    FakeEvaluationLedger,
    LedgerError,
    LedgerPolicy,
    LedgerState,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    transitioned,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator
    from pathlib import Path

    from vs_core.api import Request
    from vs_slurm.api import ClusterTarget, LedgerEntry

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="test")
)
POLL_S = 5.0
STAGES = ("accuracy", "benchmark")
DEADLINE_AT = 1_000_000.0
"""A step limit no case reaches: they end work by a cancel, a kill or a clear."""


class FlakyCluster(ScenarioCluster):
    """A scenario cluster that can stop answering, as an unreachable login node does."""

    def __init__(self) -> None:
        super().__init__()
        self.refusing = False

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        if self.refusing:
            return ClusterUnknown(operation_id=None, reason="connection refused")
        return super().inspect(target, by_job_id=by_job_id)

    def live_jobs(self) -> int:
        """How many accepted jobs the scheduler still holds, read from the scheduler itself."""
        return sum(self.has_live_job(operation_id) for operation_id in self.submissions)


class FlakyLedger(FakeEvaluationLedger):
    """A Fake ledger that can stop answering, as the cluster's filesystem does when it is lost."""

    refusing = False

    def read(self, key: str) -> LedgerEntry | None:
        self._check()
        return super().read(key)

    def _check(self) -> None:
        if self.refusing:
            message = "the ledger is unreachable"
            raise LedgerError(message)


class Process:
    """One executor stack and the runner over it: what a restart replaces."""

    def __init__(self, world: World, stack: Stack, holder: str) -> None:
        self.stack = stack
        self.holder = holder
        self.runner = SlurmJobStepRunner(
            stack.executor,
            SlurmScheduler(world.ledger, world.cluster, world.blocking),
            world.books_of(holder),
            clock=world.clock,
            poll_interval_s=POLL_S,
        )


class World:
    """One cluster and one ledger that outlive every process built over them.

    A process that dies leaves its executor's tasks as they were, never closed: the next
    process finds what the cluster and the ledger hold, and nothing else.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = VirtualClock(1000.0)
        self.ledger = FlakyLedger(project=PROJECT, clock=ManualClock())
        self.cluster = FlakyCluster()
        self.blocking = DetachedBlockingRunner()
        self.stores: dict[str, KeyedDocuments] = {}
        self.processes: list[Process] = []
        self.first = self.run(self.spawn())

    def store_of(self, holder: str) -> KeyedDocuments:
        """The receipt store of run *holder*: on its disk, so a restarted process of it shares it."""
        if holder not in self.stores:
            directory = self.root / "receipts" / holder
            directory.mkdir(parents=True, exist_ok=True)
            self.stores[holder] = receipt_store_in(directory)
        return self.stores[holder]

    def books_of(self, holder: str) -> JobBooks:
        """The job registry and evidence ledger of run *holder*, over its receipt store."""
        store = self.store_of(holder)
        return JobBooks(JobRegistry(store), ReceiptEvidenceLedger(store))

    def run[T](self, main: Coroutine[Any, Any, T]) -> T:
        """Run to the end on the virtual clock."""
        return run_virtual(self.clock, main)

    async def spawn(self, holder: str = "run-1") -> Process:
        """A new process of run *holder* over the same cluster, ledger and state, with fresh executor memory."""
        attachment = SlurmLedgerAttachment(
            ledger=self.ledger,
            config=CONFIG,
            project=PROJECT,
            holder=holder,
            policy=LedgerPolicy(),
            clock=self.clock,
        )
        stack = await build_stack(self.root, self.cluster, sleeper=self.clock, ledger=attachment)
        process = Process(self, stack, holder)
        self.processes.append(process)
        return process

    async def close(self) -> None:
        """End every process's executor, newest first, as the test's own shutdown."""
        for process in reversed(self.processes):
            await process.stack.executor.close()

    def submission(self, tag: RequestId, stages: tuple[str, ...] = STAGES) -> Request:
        """The measurement step of ``tag``: its content is the plan's, never the tag's."""
        override = plan(stages=stages, candidate=self.first.stack.snapshot)
        return submission(tag.root, override=override)

    def step(self, tag: RequestId, *, ends: bool) -> Request:
        """A step whose job ends by itself (``ends``) or stays in the queue until cancelled."""
        self.cluster.states = (SlurmJobStatus.COMPLETED,) if ends else (SlurmJobStatus.PENDING,)
        return self.submission(tag)

    async def submitted(self) -> int:
        """How many jobs the cluster has accepted, read from the cluster."""
        return len(self.cluster.submissions)

    async def live(self) -> int:
        """How many jobs the scheduler holds, read from the scheduler."""
        return self.cluster.live_jobs()

    async def pause(self, process: Process, tag: RequestId, attempt: int) -> None:
        """Pause the step through its own run, as a Pause does, and see its job left alone."""
        cancel = CancelSignal()
        step = self.step(tag, ends=False)
        task = asyncio.ensure_future(process.runner.run(step, tag, attempt, DEADLINE_AT, cancel))
        await yield_until(self.live, lambda jobs: jobs > 0 or task.done(), str(tag))
        cancel.set(CancelReason.PAUSE_NOW)
        assert await task == Cancelled(reason=CancelReason.PAUSE_NOW)
        assert self.cluster.live_jobs() == 1

    async def die_mid_run(self, process: Process, tag: RequestId, attempt: int) -> None:
        """Submit the step's job, then lose the process: only the task awaiting it is cancelled."""
        step = self.step(tag, ends=False)
        task = asyncio.ensure_future(
            process.runner.run(step, tag, attempt, DEADLINE_AT, CancelSignal())
        )
        await yield_until(self.submitted, lambda jobs: jobs > 0 or task.done(), str(tag))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def tag_of(name: str) -> RequestId:
    return RequestId(root=name)


@contextmanager
def unreachable(world: World) -> Iterator[None]:
    world.cluster.refusing = world.ledger.refusing = True
    try:
        yield
    finally:
        world.cluster.refusing = world.ledger.refusing = False


class TestSlurmJobStepRunner(StepRunnerContract):
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        world = World(self.root)

        async def left_running(_tag: RequestId) -> int:
            return world.cluster.live_jobs()

        return StepRunnerUnderTest(
            runner=world.first.runner,
            run=world.run,
            finishing_step=lambda tag: world.step(tag, ends=True),
            running_step=lambda tag: world.step(tag, ends=False),
            left_running=left_running,
            unreachable=lambda: unreachable(world),
        )


def test_a_restarted_process_adopts_the_job_a_killed_one_submitted_and_returns_its_result(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    tag = tag_of("killed")

    async def main() -> None:
        await world.die_mid_run(world.first, tag, attempt=1)
        assert len(world.cluster.submissions) == 1
        second = await world.spawn()
        assert await second.runner.clear(world.submission(tag), tag) == Adopted(None)
        world.cluster.finish_jobs()
        result = await second.runner.run(
            world.step(tag, ends=True), tag, 2, DEADLINE_AT, CancelSignal()
        )
        assert isinstance(result, Finished)
        assert result.observation.status is ObservationStatus.SUCCEEDED
        assert result.observation.request_id == tag
        assert result.report.evidence
        assert result.report.evaluation_result is not None
        # Core takes the report as the job's last observation, so the readings are in the ledger
        # the operation that decodes them reads by the time the step reports (not after a poll).
        ledger = ReceiptEvidenceLedger(world.store_of("run-1"))
        assert all(ledger.lookup(ref.key) is not None for ref in result.report.evidence)
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 1


def test_clear_of_a_step_whose_content_the_cluster_never_saw_is_cleared_after_a_restart(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)

    async def main() -> None:
        await world.die_mid_run(world.first, tag_of("seen"), attempt=1)
        second = await world.spawn()
        other = world.submission(tag_of("unseen"), stages=("accuracy",))
        assert await second.runner.clear(other, tag_of("unseen")) == Cleared()
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 1


def test_a_step_of_the_content_another_step_submitted_attaches_to_its_job(tmp_path: Path) -> None:
    """The key names the content, not the tag: one content is one job, whoever asked first."""
    world = World(tmp_path)
    first, second_tag = tag_of("first"), tag_of("second")

    async def main() -> None:
        await world.die_mid_run(world.first, first, attempt=1)
        second = await world.spawn()
        assert await second.runner.clear(world.submission(second_tag), second_tag) == Adopted(None)
        world.cluster.finish_jobs()
        result = await second.runner.run(
            world.step(second_tag, ends=True), second_tag, 1, DEADLINE_AT, CancelSignal()
        )
        assert isinstance(result, Finished)
        assert result.observation.request_id == second_tag
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 1


@pytest.mark.parametrize("reason", [CancelReason.PAUSE_NOW, CancelReason.DRAIN_EXPIRED])
def test_a_pause_leaves_the_job_running_and_the_next_attempt_adopts_it(
    tmp_path: Path, reason: CancelReason
) -> None:
    """A cancel would throw away the queue time and the work done; the job survives the host."""
    world = World(tmp_path)
    runner = world.first.runner
    tag = tag_of("paused")

    async def main() -> None:
        cancel = CancelSignal()
        task = asyncio.ensure_future(
            runner.run(world.step(tag, ends=False), tag, 1, DEADLINE_AT, cancel)
        )
        await yield_until(world.submitted, lambda jobs: jobs == 1, "the first job")
        cancel.set(reason)
        assert await task == Cancelled(reason=reason)
        assert world.cluster.live_jobs() == 1
        assert await runner.clear(world.submission(tag), tag) == Adopted(None)
        world.cluster.finish_jobs()
        again = await runner.run(world.step(tag, ends=True), tag, 2, DEADLINE_AT, CancelSignal())
        assert isinstance(again, Finished)
        assert again.observation.status is ObservationStatus.SUCCEEDED
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 1
    assert world.cluster.cancelled == []


@pytest.mark.parametrize(
    "reason", [reason for reason in CancelReason if not interruption_of(reason)]
)
def test_a_cancel_that_ends_the_step_cancels_the_job_and_the_next_attempt_submits_a_new_one(
    tmp_path: Path, reason: CancelReason
) -> None:
    """The cancel ended the job; attaching to it would return Cancelled, so the next one is new."""
    world = World(tmp_path)
    runner = world.first.runner
    tag = tag_of("ended")

    async def main() -> None:
        cancel = CancelSignal()
        task = asyncio.ensure_future(
            runner.run(world.step(tag, ends=False), tag, 1, DEADLINE_AT, cancel)
        )
        await yield_until(world.submitted, lambda jobs: jobs == 1, "the first job")
        cancel.set(reason)
        assert await task == Cancelled(reason=reason)
        assert world.cluster.live_jobs() == 0
        assert await runner.clear(world.submission(tag), tag) == Cleared()
        again = await runner.run(world.step(tag, ends=True), tag, 2, DEADLINE_AT, CancelSignal())
        assert isinstance(again, Finished)
        assert again.observation.status is ObservationStatus.SUCCEEDED
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 2
    assert world.cluster.live_jobs() == 0


def listed(world: World, step: Request) -> tuple[str, ...]:
    """The jobs a close of the step's scope lists as its children: every handle indexed under it."""
    return world.books_of("run-1").registry.handles(step.scope, step.admission_id)


@pytest.mark.parametrize(
    "reason", [reason for reason in CancelReason if not interruption_of(reason)]
)
def test_a_step_a_cancel_ended_leaves_its_scope_no_job_to_list(
    tmp_path: Path, reason: CancelReason
) -> None:
    """The step ends `Cancelled` with no job in its report: a close that listed it names a job
    core never heard of, and the scope would wait for a release nothing proves."""
    world = World(tmp_path)
    runner = world.first.runner
    tag = tag_of("ended")
    step = world.step(tag, ends=False)

    async def main() -> None:
        cancel = CancelSignal()
        task = asyncio.ensure_future(runner.run(step, tag, 1, DEADLINE_AT, cancel))
        await yield_until(world.submitted, lambda jobs: jobs == 1, "the first job")
        assert len(listed(world, step)) == 1
        cancel.set(reason)
        assert await task == Cancelled(reason=reason)
        assert listed(world, step) == ()
        await world.close()

    world.run(main())


@pytest.mark.parametrize("reason", [CancelReason.PAUSE_NOW, CancelReason.DRAIN_EXPIRED])
def test_a_pause_leaves_the_job_listed_for_the_next_attempt_to_adopt(
    tmp_path: Path, reason: CancelReason
) -> None:
    world = World(tmp_path)
    runner = world.first.runner
    tag = tag_of("paused")
    step = world.step(tag, ends=False)

    async def main() -> None:
        cancel = CancelSignal()
        task = asyncio.ensure_future(runner.run(step, tag, 1, DEADLINE_AT, cancel))
        await yield_until(world.submitted, lambda jobs: jobs == 1, "the first job")
        cancel.set(reason)
        assert await task == Cancelled(reason=reason)
        assert len(listed(world, step)) == 1
        await world.close()

    world.run(main())


def test_a_step_cleared_of_every_job_lists_none(tmp_path: Path) -> None:
    """A withdrawn request whose job ended left a handle indexed, and nothing of it lives."""
    world = World(tmp_path)
    left = tag_of("left")
    step = world.submission(left)

    async def main() -> None:
        await world.die_mid_run(world.first, left, attempt=1)
        assert len(listed(world, step)) == 1
        _withdrawn(world, left)
        second = await world.spawn()
        assert await second.runner.clear(step, left) == Cleared()
        assert listed(world, step) == ()
        await world.close()

    world.run(main())


@pytest.mark.parametrize("reason", list(CancelReason))
def test_a_step_cancelled_before_its_run_began_submits_nothing(
    tmp_path: Path, reason: CancelReason
) -> None:
    """A closing scope withdraws a step that waits to start: its job is never submitted after."""
    world = World(tmp_path)
    runner = world.first.runner
    tag = tag_of("early")

    async def main() -> None:
        cancel = CancelSignal()
        cancel.set(reason)
        assert await runner.run(
            world.step(tag, ends=False), tag, 1, DEADLINE_AT, cancel
        ) == Cancelled(reason=reason)
        assert await runner.clear(world.submission(tag), tag) == Cleared()
        await world.close()

    world.run(main())
    assert world.cluster.submissions == []
    assert world.cluster.cancelled == []


def test_a_withdrawn_step_of_the_content_another_step_submitted_leaves_that_job_alone(
    tmp_path: Path,
) -> None:
    """The step found the job by its content, not by its tag: its withdrawal holds nothing to cancel."""
    world = World(tmp_path)
    first, second_tag = tag_of("first"), tag_of("second")

    async def main() -> None:
        await world.die_mid_run(world.first, first, attempt=1)
        runner = (await world.spawn()).runner
        assert await runner.clear(world.submission(second_tag), second_tag) == Adopted(None)
        cancel = CancelSignal()
        cancel.set(CancelReason.WITHDRAWN)
        withdrawn = await runner.run(
            world.step(second_tag, ends=False), second_tag, 1, DEADLINE_AT, cancel
        )
        assert withdrawn == Cancelled(reason=CancelReason.WITHDRAWN)
        assert len(world.cluster.submissions) == 1
        assert world.cluster.live_jobs() == 1
        assert world.cluster.cancelled == []
        await world.close()

    world.run(main())


def test_any_order_of_kills_pauses_restarts_and_clears_runs_one_job_and_cancels_none(
    tmp_path: Path,
) -> None:
    """One tag is one job: a kill, a pause or a clear never duplicates it and never cancels it."""
    for seed in range(examples(pr=6, nightly=40)):
        world = World(tmp_path / f"world-{seed}")
        world.run(_a_sequence_ends_with_a_result(world, seed))
        assert len(world.cluster.submissions) == 1, seed
        assert world.cluster.cancelled == [], seed


async def _a_sequence_ends_with_a_result(world: World, seed: int) -> None:
    """Run a generated sequence, then finish the step."""
    tag = tag_of(f"sequence-{seed}")
    rng = SeededRandom(seed)
    current = world.first
    for attempt in range(1, rng.randint(2, 7)):
        match rng.choice(("kill", "pause", "restart", "clear")):
            case "kill":
                await world.die_mid_run(current, tag, attempt)
            case "pause":
                await world.pause(current, tag, attempt)
            case "restart":
                current = await world.spawn()
            case _:
                await current.runner.clear(world.submission(tag), tag)
    current = await world.spawn()
    world.cluster.finish_jobs()
    result = await current.runner.run(
        world.step(tag, ends=True), tag, 99, DEADLINE_AT, CancelSignal()
    )
    assert isinstance(result, Finished), seed
    await world.close()


def test_a_step_that_reuses_a_finished_submission_launches_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    runner = world.first.runner
    source, reusing = tag_of("source"), tag_of("reusing")

    async def main() -> None:
        first = await runner.run(
            world.step(source, ends=True), source, 1, DEADLINE_AT, CancelSignal()
        )
        assert isinstance(first, Finished)
        step = world.step(reusing, ends=True).model_copy(update={"reuses": source})
        again = await runner.run(step, reusing, 1, DEADLINE_AT, CancelSignal())
        assert isinstance(again, Finished)
        assert again.observation.status is ObservationStatus.SUCCEEDED
        assert again.observation.request_id == reusing
        assert again.report.evidence
        assert {item.reused_from for item in again.report.evidence} == {source}
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 1


def test_a_step_that_reuses_a_submission_not_seen_ended_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path)
    reusing = tag_of("reusing")

    async def main() -> None:
        step = world.step(reusing, ends=True).model_copy(update={"reuses": tag_of("nobody")})
        result = await world.first.runner.run(step, reusing, 1, DEADLINE_AT, CancelSignal())
        assert isinstance(result, Finished)
        assert result.observation.status is ObservationStatus.REJECTED
        assert result.report.measurement_failure is MeasurementFailure.INFRASTRUCTURE
        await world.close()

    world.run(main())
    assert world.cluster.submissions == []


def _withdrawn(world: World, tag: RequestId) -> str:
    """The ledger records the step's request withdrawn, as a withdrawal does once it asked for the
    cancel, without the job having ended. Returns the operation of the attempt."""
    (key,) = world.ledger.list_keys()
    entry = world.ledger.read(key)
    assert entry is not None
    withdrawn = transitioned(entry.record, LedgerState.WITHDRAWN, reason=f"withdrawn for {tag}")
    assert world.ledger.replace(entry, withdrawn)
    return entry.record.operation_id


def test_a_withdrawn_request_whose_job_still_runs_is_cleared_only_once_that_job_ended(
    tmp_path: Path,
) -> None:
    """The ledger records a withdrawal after the cancel was asked for, not after the job ended."""
    world = World(tmp_path)
    left, other = tag_of("left"), tag_of("other-step-of-the-content")

    async def main() -> None:
        await world.die_mid_run(world.first, left, attempt=1)
        operation = _withdrawn(world, left)
        assert world.cluster.live_jobs() == 1
        second = await world.spawn()
        assert await second.runner.clear(world.submission(other), other) == Cleared()
        assert world.cluster.live_jobs() == 0
        assert operation in world.cluster.cancelled
        await world.close()

    world.run(main())


def test_a_withdrawn_request_is_not_cleared_while_the_scheduler_cannot_say_what_became_of_it(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    left, other = tag_of("left"), tag_of("other-step-of-the-content")

    async def main() -> None:
        await world.die_mid_run(world.first, left, attempt=1)
        _withdrawn(world, left)
        second = await world.spawn()
        world.cluster.refusing = True
        with pytest.raises(ClearUnavailableError):
            await second.runner.clear(world.submission(other), other)
        world.cluster.refusing = False
        assert world.cluster.live_jobs() == 1
        await world.close()

    world.run(main())


def test_a_step_whose_job_is_on_record_is_never_cleared_because_the_workspace_lost_its_commit(
    tmp_path: Path,
) -> None:
    """``has_revision`` says what the workspace holds, not whether the step submitted a job."""
    world = World(tmp_path)
    tag = tag_of("submitted")

    async def main() -> None:
        await world.die_mid_run(world.first, tag, attempt=1)
        lost = world.submission(tag).model_copy(
            update={"plan": plan(stages=STAGES, candidate="0" * 40)}
        )
        # Its handle is on record, but neither process can read the job's state yet: the plan
        # cannot be keyed, so the ledger cannot say, and "never ran" is not an answer.
        with pytest.raises(ClearUnavailableError):
            await world.first.runner.clear(lost, tag)
        second = await world.spawn()
        with pytest.raises(ClearUnavailableError):
            await second.runner.clear(lost, tag)
        assert world.cluster.live_jobs() == 1
        await world.close()

    world.run(main())


def test_a_step_that_never_submitted_is_cleared_when_its_plan_could_never_have_run(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    tag = tag_of("never")

    async def main() -> None:
        lost = world.submission(tag).model_copy(
            update={"plan": plan(stages=STAGES, candidate="0" * 40)}
        )
        assert await world.first.runner.clear(lost, tag) == Cleared()
        await world.close()

    world.run(main())


def test_one_runs_cancel_never_ends_a_job_another_run_still_wants(tmp_path: Path) -> None:
    """Two runs of one cluster share the job of one content: the ledger keeps it for the other."""
    world = World(tmp_path)
    mine, theirs = tag_of("mine"), tag_of("theirs")

    async def main() -> None:
        other_run = await world.spawn("run-2")
        owner = asyncio.ensure_future(
            world.first.runner.run(
                world.step(theirs, ends=False), theirs, 1, DEADLINE_AT, CancelSignal()
            )
        )
        await yield_until(world.submitted, lambda jobs: jobs == 1, "run-1's job")
        cancel = CancelSignal()
        attached = asyncio.ensure_future(
            other_run.runner.run(world.step(mine, ends=False), mine, 1, DEADLINE_AT, cancel)
        )
        await yield_until(world.live, lambda jobs: jobs == 1, "the shared job")
        cancel.set(CancelReason.WITHDRAWN)
        assert await attached == Cancelled(reason=CancelReason.WITHDRAWN)
        assert world.cluster.live_jobs() == 1
        assert world.cluster.cancelled == []
        world.cluster.finish_jobs()
        result = await owner
        assert isinstance(result, Finished)
        assert len(world.cluster.submissions) == 1
        await world.close()

    world.run(main())


@pytest.mark.parametrize("ledger", ["holds the request", "holds it withdrawn"])
def test_a_cancelled_clear_returns_only_after_the_ledger_and_scheduler_reads_it_started_ended(
    tmp_path: Path, ledger: str
) -> None:
    """The runner's own thread work is the ledger read and the scheduler calls of ``clear``.

    The contract's flows never reach them (a restarted process polls the tag's job and adopts
    it), so a clear of another step of the same content, which only the ledger can answer, is
    cancelled while that work is in flight.
    """
    world = World(tmp_path)
    left, other = tag_of("left"), tag_of("other-step-of-the-content")

    async def main() -> None:
        await world.die_mid_run(world.first, left, attempt=1)
        if ledger == "holds it withdrawn":
            _withdrawn(world, left)
        second = await world.spawn()
        world.blocking.hold()
        call = asyncio.ensure_future(second.runner.clear(world.submission(other), other))

        async def in_flight() -> int:
            return world.blocking.in_flight

        await yield_until(in_flight, lambda held: held > 0 or call.done(), "the ledger read")
        assert world.blocking.in_flight > 0
        call.cancel()
        for _ in range(20):
            await yield_now()
        assert not call.done(), "clear returned while its thread work was in flight"
        world.blocking.release()
        await asyncio.gather(call, return_exceptions=True)
        assert world.blocking.in_flight == 0
        await world.close()

    world.run(main())


# The evaluation kinds on the ExecutorStepRunner serve the job this runner submitted


class Served:
    """The evaluation executor core's other job requests reach, over the world's own cluster."""

    def __init__(self, world: World) -> None:
        evaluation = MeasurementRequests(
            world.first.stack.executor, world.store_of(world.first.holder)
        )
        self.runner = ExecutorStepRunner(
            RequestExecutors(evaluation=evaluation),
            DECLARED_REPEATS,
            fence=HostFence(host_id=HostId(root="host"), epoch=1),
            lease=RevocableLease(),
            clock=world.clock,
        )

    async def run(self, request: Request) -> Finished:
        assert request.request_id is not None
        done = await self.runner.run(request, request.request_id, 1, DEADLINE_AT, CancelSignal())
        assert isinstance(done, Finished)
        return done


def job_request(
    kind: type[ObserveOwnedJob] | type[CollectEvidence], step: Request, resource: str
) -> ObserveOwnedJob | CollectEvidence:
    return kind(
        request_id=RequestId(root=f"{kind.__name__}-of-{resource}"),
        scope=step.scope,
        admission_id=step.admission_id,
        resource_id=ResourceId(root=resource),
    )


def test_a_job_the_runner_submitted_is_collected_and_observed_through_the_executor_runner(
    tmp_path: Path,
) -> None:
    """Core asks to collect the evidence of an ended job that brought none, and to poll a live one."""
    world = World(tmp_path / "world")
    tag = tag_of("submitted")

    async def main() -> None:
        served = Served(world)
        step = world.step(tag, ends=True)
        ended = await world.first.runner.run(step, tag, 1, DEADLINE_AT, CancelSignal())
        assert isinstance(ended, Finished)
        resource = ended.observation.resource_id
        assert resource is not None
        for kind in (CollectEvidence, ObserveOwnedJob):
            done = await served.run(job_request(kind, step, resource.root))
            assert done.observation.status is ObservationStatus.SUCCEEDED, kind.__name__
            assert done.observation.terminal
        await world.close()

    world.run(main())


def test_a_scope_closed_over_a_running_job_the_runner_submitted_cancels_it_and_lists_it(
    tmp_path: Path,
) -> None:
    world = World(tmp_path / "world")
    tag = tag_of("running")

    async def main() -> None:
        served = Served(world)
        step = world.step(tag, ends=False)
        run = asyncio.ensure_future(
            world.first.runner.run(step, tag, 1, DEADLINE_AT, CancelSignal())
        )
        await yield_until(world.live, lambda jobs: jobs > 0 or run.done(), str(tag))
        close = CloseAttemptScope(
            request_id=RequestId(root="close"),
            scope=step.scope,
            admission_id=step.admission_id,
            attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
        )
        done = await served.run(close)
        assert len(done.observation.children) == 1
        assert world.cluster.live_jobs() == 0
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await world.close()

    world.run(main())
