"""LocalJobStepRunner passes the StepRunner contract over the in-process executor and its Fakes.

The runner sits on the production polling executor with only the trusted evaluation effects and
the workspaces faked. A step that runs until cancelled holds its benchmark call at a gate; a
step that finishes by itself runs the accuracy stage, which no gate holds. The stage work of a
dead process is a Fake reaper's ``leave``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from vs_core.api import ObservationStatus, RequestId
from vs_core.api.recovery import Cancelled, CancelReason, Finished
from vs_evaluation.api import ResourceRequirements
from vs_runtime._evaluation_jobs import handle_of_life
from vs_runtime.api import AccuracyEvaluation, PollingEvaluationExecutor
from vs_runtime.api.core import JobRegistry
from vs_runtime.api.evaluation_step_runners import JobStepRun, LocalJobStepRunner
from vs_runtime.api.executor_contexts import receipt_store_in
from vs_runtime.api.slurm_evaluation_stack import plan, submission
from vs_runtime.api.step_runner import CancelSignal, Cleared
from vs_runtime.api.testing import (
    FakeCandidateWorkspace,
    FakeEvaluation,
    FakeStageWorkReaper,
    FakeWorkspace,
    FakeWorkspaces,
    StepRunnerContract,
    StepRunnerUnderTest,
)
from vs_sim.api.testing import VirtualClock, run_virtual, yield_until

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

    from vs_core.api import Request

GATED_BENCHMARKS = 32
"""Every benchmark call a test makes is held, so a step with a benchmark stage runs until cancelled."""
POLL_S = 5.0
DEADLINE_AT = 1_000_000.0
"""A step limit no case reaches: they end work by a cancel, a kill or a clear."""


class World:
    """One host: an evaluation executor over Fakes, a reaper, and the runner over both."""

    def __init__(self, root: Path) -> None:
        self.clock = VirtualClock(1000.0)
        self.evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
        self.gates = [self.evaluation.gate("benchmark", call) for call in range(GATED_BENCHMARKS)]
        self.workspaces = FakeWorkspaces(
            FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
        )
        self.executor = PollingEvaluationExecutor(
            self.evaluation, self.workspaces, clock=self.clock
        )
        self.reaper = FakeStageWorkReaper()
        self.registry = JobRegistry(receipt_store_in(root))
        job = JobStepRun(
            self.executor,
            self.registry,
            clock=self.clock,
            poll_interval_s=POLL_S,
            outlives_host=False,
        )
        self.runner = LocalJobStepRunner(self.executor, self.reaper, job)
        self.snapshot = self.run(self.workspaces.root.snapshot("candidate"))

    def run[T](self, main: Coroutine[Any, Any, T]) -> T:
        return run_virtual(self.clock, main)

    def step(self, tag: RequestId, *, ends: bool) -> Request:
        """A step that ends by itself (an accuracy stage) or runs until cancelled (a benchmark)."""
        stages = ("accuracy",) if ends else ("benchmark",)
        return submission(tag.root, override=plan(stages=stages, candidate=self.snapshot))

    async def left_running(self, tag: RequestId) -> int:
        """Evaluation tasks the executor still runs, and stage work a dead process left."""
        snapshot = await self.executor.availability(ResourceRequirements())
        return snapshot.in_flight + int(tag in self.reaper.alive)

    async def die_mid_run(self, tag: RequestId, attempt: int) -> None:
        """Start the step and lose the caller awaiting it; the evaluation task goes on."""
        task = asyncio.ensure_future(
            self.runner.run(self.step(tag, ends=False), tag, attempt, DEADLINE_AT, CancelSignal())
        )
        await yield_until(
            lambda: self.left_running(tag), lambda running: running > 0 or task.done(), str(tag)
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@contextmanager
def unreachable(world: World) -> Iterator[None]:
    world.reaper.refusing = True
    try:
        yield
    finally:
        world.reaper.refusing = False


def tag_of(name: str) -> RequestId:
    return RequestId(root=name)


class TestLocalJobStepRunner(StepRunnerContract):
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        world = World(self.root)
        return StepRunnerUnderTest(
            runner=world.runner,
            run=world.run,
            finishing_step=lambda tag: world.step(tag, ends=True),
            running_step=lambda tag: world.step(tag, ends=False),
            left_running=world.left_running,
            unreachable=lambda: unreachable(world),
        )


def test_a_killed_attempt_is_cancelled_by_clear_and_the_next_attempt_runs_a_new_evaluation(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    tag = tag_of("killed")

    async def main() -> None:
        await world.die_mid_run(tag, attempt=1)
        assert await world.left_running(tag) == 1
        assert await world.runner.clear(world.step(tag, ends=False), tag) == Cleared()
        assert await world.left_running(tag) == 0
        again = await world.runner.run(
            world.step(tag, ends=True), tag, 2, DEADLINE_AT, CancelSignal()
        )
        assert isinstance(again, Finished)
        assert again.observation.status is ObservationStatus.SUCCEEDED

    world.run(main())
    first, *_ = world.evaluation.benchmark_calls
    assert isinstance(first.workspace, FakeCandidateWorkspace)
    assert first.workspace.discarded
    assert len(world.evaluation.accuracy_calls) == 1


def test_clear_reaps_the_stage_work_a_dead_process_left_by_the_tag_alone(tmp_path: Path) -> None:
    world = World(tmp_path)
    left, other = tag_of("left"), tag_of("other")
    world.reaper.leave(left)
    world.reaper.leave(other)

    async def main() -> None:
        assert await world.runner.clear(world.step(left, ends=False), left) == Cleared()
        assert await world.left_running(left) == 0
        assert await world.left_running(other) == 1

    world.run(main())
    assert world.reaper.reaped == [left]


def test_a_cancelled_step_returns_cancelled_and_the_same_tag_then_runs_clean(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    tag = tag_of("paused")

    async def main() -> None:
        cancel = CancelSignal()
        task = asyncio.ensure_future(
            world.runner.run(world.step(tag, ends=False), tag, 1, DEADLINE_AT, cancel)
        )
        await yield_until(
            lambda: world.left_running(tag), lambda running: running == 1, "the evaluation"
        )
        cancel.set(CancelReason.DRAIN_EXPIRED)
        assert await task == Cancelled(reason=CancelReason.DRAIN_EXPIRED)
        again = await world.runner.run(
            world.step(tag, ends=True), tag, 2, DEADLINE_AT, CancelSignal()
        )
        assert isinstance(again, Finished)
        assert again.observation.status is ObservationStatus.SUCCEEDED

    world.run(main())


def test_a_step_that_reuses_a_finished_submission_launches_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    source, reusing = tag_of("source"), tag_of("reusing")

    async def main() -> None:
        first = await world.runner.run(
            world.step(source, ends=True), source, 1, DEADLINE_AT, CancelSignal()
        )
        assert isinstance(first, Finished)
        step = world.step(reusing, ends=True).model_copy(update={"reuses": source})
        again = await world.runner.run(step, reusing, 1, DEADLINE_AT, CancelSignal())
        assert isinstance(again, Finished)
        assert again.observation.request_id == reusing
        assert {item.reused_from for item in again.report.evidence} == {source}

    world.run(main())
    assert len(world.evaluation.accuracy_calls) == 1


def test_the_evaluation_opens_its_candidate_under_the_mark_of_its_handle(tmp_path: Path) -> None:
    world = World(tmp_path)
    tag = tag_of("marked")

    async def main() -> None:
        step = world.step(tag, ends=True)
        await world.runner.run(step, tag, 1, DEADLINE_AT, CancelSignal())
        await world.runner.clear(step, tag)
        await world.runner.run(step, tag, 2, DEADLINE_AT, CancelSignal())

    world.run(main())
    assert world.workspaces.measured_step_tags[-2:] == (
        handle_of_life(tag, 0),
        handle_of_life(tag, 1),
    )
