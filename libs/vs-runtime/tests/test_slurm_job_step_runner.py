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

from vs_core.api import MeasurementFailure, ObservationStatus, RequestId
from vs_core.api.recovery import Cancelled, CancelReason, Finished
from vs_runtime.api.evaluation_step_runners import SlurmJobStepRunner, SlurmScheduler
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
from vs_sim.api import SeededRandom
from vs_sim.api.testing import (
    InlineBlockingRunner,
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

    def __init__(self, world: World, stack: Stack) -> None:
        self.stack = stack
        self.runner = SlurmJobStepRunner(
            stack.executor,
            SlurmScheduler(world.ledger, world.cluster, InlineBlockingRunner()),
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
        self.processes: list[Process] = []
        self.first = self.run(self.spawn())

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
        process = Process(self, stack)
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
        """Cancel the step's job through its own run, as a Pause does, and see it end."""
        cancel = CancelSignal()
        step = self.step(tag, ends=False)
        task = asyncio.ensure_future(process.runner.run(step, tag, attempt, DEADLINE_AT, cancel))
        await yield_until(self.live, lambda jobs: jobs > 0 or task.done(), str(tag))
        cancel.set(CancelReason.PAUSE_NOW)
        assert await task == Cancelled(reason=CancelReason.PAUSE_NOW)

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


def test_a_cancelled_job_is_never_attached_to_so_the_next_attempt_submits_a_new_one(
    tmp_path: Path,
) -> None:
    """The cancel interrupted the attempt and left no result; attaching would return Cancelled."""
    world = World(tmp_path)
    runner = world.first.runner
    tag = tag_of("paused")

    async def main() -> None:
        cancel = CancelSignal()
        task = asyncio.ensure_future(
            runner.run(world.step(tag, ends=False), tag, 1, DEADLINE_AT, cancel)
        )
        await yield_until(world.submitted, lambda jobs: jobs == 1, "the first job")
        cancel.set(CancelReason.PAUSE_NOW)
        assert await task == Cancelled(reason=CancelReason.PAUSE_NOW)
        assert await runner.clear(world.submission(tag), tag) == Cleared()
        again = await runner.run(world.step(tag, ends=True), tag, 2, DEADLINE_AT, CancelSignal())
        assert isinstance(again, Finished)
        assert again.observation.status is ObservationStatus.SUCCEEDED
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 2
    assert world.cluster.live_jobs() == 0


def test_any_order_of_kills_pauses_restarts_and_clears_leaves_one_job_that_was_not_cancelled(
    tmp_path: Path,
) -> None:
    """One tag is one live job: every other job it ever had was cancelled, never duplicated."""
    for seed in range(examples(pr=6, nightly=40)):
        world = World(tmp_path / f"world-{seed}")
        world.run(_a_sequence_ends_with_a_result(world, seed))
        submitted = set(world.cluster.submissions)
        cancelled = set(world.cluster.cancelled)
        assert cancelled <= submitted, seed
        assert len(submitted - cancelled) == 1, seed


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
        cancel.set(CancelReason.PAUSE_NOW)
        assert await attached == Cancelled(reason=CancelReason.PAUSE_NOW)
        assert world.cluster.live_jobs() == 1
        assert world.cluster.cancelled == []
        world.cluster.finish_jobs()
        result = await owner
        assert isinstance(result, Finished)
        assert len(world.cluster.submissions) == 1
        await world.close()

    world.run(main())
