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
from vs_runtime.api.evaluation_step_runners import SlurmJobStepRunner
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, Stack, build_stack, submission
from vs_runtime.api.step_runner import Adopted, CancelSignal, Cleared
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
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
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

    def bound_key(self, holder: str, handle: str) -> str | None:
        self._check()
        return super().bound_key(holder, handle)

    def _check(self) -> None:
        if self.refusing:
            message = "the ledger is unreachable"
            raise LedgerError(message)


class Process:
    """One executor stack and the runner over it: what a restart replaces."""

    def __init__(self, world: World, stack: Stack, attachment: SlurmLedgerAttachment) -> None:
        self.stack = stack
        self.runner = SlurmJobStepRunner(
            stack.executor,
            attachment,
            InlineBlockingRunner(),
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

    async def spawn(self) -> Process:
        """A new process over the same cluster, ledger and state, with fresh executor memory."""
        attachment = SlurmLedgerAttachment(
            ledger=self.ledger,
            config=CONFIG,
            project=PROJECT,
            holder="run-1",
            policy=LedgerPolicy(),
            clock=self.clock,
        )
        stack = await build_stack(self.root, self.cluster, sleeper=self.clock, ledger=attachment)
        process = Process(self, stack, attachment)
        self.processes.append(process)
        return process

    async def close(self) -> None:
        """End every process's executor, newest first, as the test's own shutdown."""
        for process in reversed(self.processes):
            await process.stack.executor.close()

    def step(self, tag: RequestId, *, ends: bool) -> Request:
        """A step whose job ends by itself (``ends``) or stays in the queue until cancelled."""
        self.cluster.states = (SlurmJobStatus.COMPLETED,) if ends else (SlurmJobStatus.PENDING,)
        return submission(tag.root, candidate=self.first.stack.snapshot)

    async def submitted(self) -> int:
        """How many jobs the cluster has accepted, read from the cluster."""
        return len(self.cluster.submissions)

    async def die_mid_run(self, process: Process, tag: RequestId, attempt: int) -> None:
        """Submit the step's job, then lose the process: only the task awaiting it is cancelled."""
        step = self.step(tag, ends=False)
        task = asyncio.ensure_future(process.runner.run(step, tag, attempt, CancelSignal()))
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
        assert await second.runner.clear(tag) == Adopted(None)
        world.cluster.finish_jobs()
        result = await second.runner.run(world.step(tag, ends=True), tag, 2, CancelSignal())
        assert isinstance(result, Finished)
        assert result.observation.status is ObservationStatus.SUCCEEDED
        assert result.observation.request_id == tag
        assert result.report.evidence
        assert result.report.evaluation_result is not None
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 1


def test_clear_of_a_tag_the_cluster_never_saw_is_cleared_even_after_a_restart(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)

    async def main() -> None:
        await world.die_mid_run(world.first, tag_of("seen"), attempt=1)
        second = await world.spawn()
        assert await second.runner.clear(tag_of("unseen")) == Cleared()
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
        task = asyncio.ensure_future(runner.run(world.step(tag, ends=False), tag, 1, cancel))
        await yield_until(world.submitted, lambda jobs: jobs == 1, "the first job")
        cancel.set(CancelReason.PAUSE_NOW)
        assert await task == Cancelled(reason=CancelReason.PAUSE_NOW)
        assert await runner.clear(tag) == Cleared()
        again = await runner.run(world.step(tag, ends=True), tag, 2, CancelSignal())
        assert isinstance(again, Finished)
        assert again.observation.status is ObservationStatus.SUCCEEDED
        await world.close()

    world.run(main())
    assert len(world.cluster.submissions) == 2
    assert world.cluster.live_jobs() == 0


def test_any_order_of_kills_restarts_clears_and_runs_submits_the_step_once(tmp_path: Path) -> None:
    """Without a cancel, one tag is one job however often the process dies or clears."""
    for seed in range(examples(pr=6, nightly=40)):
        world = World(tmp_path / f"world-{seed}")
        world.run(_a_sequence_ends_with_one_submission(world, seed))
        assert len(world.cluster.submissions) == 1, seed


async def _a_sequence_ends_with_one_submission(world: World, seed: int) -> None:
    tag = tag_of(f"sequence-{seed}")
    rng = SeededRandom(seed)
    current = world.first
    for attempt in range(1, rng.randint(2, 6)):
        match rng.choice(("kill", "restart", "clear")):
            case "kill":
                await world.die_mid_run(current, tag, attempt)
            case "restart":
                current = await world.spawn()
            case _:
                await current.runner.clear(tag)
    current = await world.spawn()
    world.cluster.finish_jobs()
    result = await current.runner.run(world.step(tag, ends=True), tag, 99, CancelSignal())
    assert isinstance(result, Finished), seed
    await world.close()


def test_a_step_that_reuses_a_finished_submission_launches_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    runner = world.first.runner
    source, reusing = tag_of("source"), tag_of("reusing")

    async def main() -> None:
        first = await runner.run(world.step(source, ends=True), source, 1, CancelSignal())
        assert isinstance(first, Finished)
        step = world.step(reusing, ends=True).model_copy(update={"reuses": source})
        again = await runner.run(step, reusing, 1, CancelSignal())
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
        result = await world.first.runner.run(step, reusing, 1, CancelSignal())
        assert isinstance(result, Finished)
        assert result.observation.status is ObservationStatus.REJECTED
        assert result.report.measurement_failure is MeasurementFailure.INFRASTRUCTURE
        await world.close()

    world.run(main())
    assert world.cluster.submissions == []
