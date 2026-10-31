"""LocalJobStepRunner passes the StepRunner contract over the in-process executor and its Fakes.

The runner sits on the production polling executor with only the trusted evaluation effects and
the workspaces faked. A step that runs until cancelled holds its benchmark call at a gate; a
step that finishes by itself runs the accuracy stage, which no gate holds. The stage work of a
dead process is a process the Fake host's table shows, ended by the production
``ProcessStageWorkReaper``: the world starts a stage command when a measured candidate opens, ends
it when its evaluation call ends. What a host that died left is started in the table directly.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from vs_core.api import DecisionId, ObservationStatus, RequestId, SubmitMeasurement
from vs_core.api.recovery import Cancelled, CancelReason, Finished
from vs_evaluation.api import ResourceRequirements
from vs_runtime.api import AccuracyEvaluation, PollingEvaluationExecutor
from vs_runtime.api.core import (
    JobBooks,
    JobRegistry,
    MeasurementDecisions,
    MeasurementProgress,
    ReceiptEvidenceLedger,
    StageSettled,
    StageStarted,
)
from vs_runtime.api.evaluation_step_runners import LocalJobStepRunner, ProcessStageWorkReaper
from vs_runtime.api.executor_contexts import receipt_store_in
from vs_runtime.api.slurm_evaluation_stack import plan, submission
from vs_runtime.api.stage_marks import handle_of_life, is_stage_mark_of, stage_mark
from vs_runtime.api.step_runner import CancelSignal, Cleared
from vs_runtime.api.testing import (
    FakeCandidateWorkspace,
    FakeEvaluation,
    FakeWorkspace,
    FakeWorkspaces,
    StepRunnerContract,
    StepRunnerUnderTest,
)
from vs_sandbox.api import STEP_TAG_ENV
from vs_sim.api.testing import (
    DetachedBlockingRunner,
    FakeProcessTable,
    Shows,
    VirtualClock,
    run_virtual,
    yield_until,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

    from vs_core.api import Request
    from vs_runtime.api import (
        AccuracyReceipt,
        BenchmarkEvaluation,
        BenchmarkObjective,
        CandidateWorkspace,
        Workspace,
    )
    from vs_sim.api import MarkedProcess, OutputSink

GATED_BENCHMARKS = 32
"""Every benchmark call a test makes is held, so a step with a benchmark stage runs until cancelled."""
POLL_S = 5.0
DEADLINE_AT = 1_000_000.0
"""A step limit no case reaches: they end work by a cancel, a kill or a clear."""


RUN = "run-a"


class HostWorkspaces(FakeWorkspaces):
    """Workspaces whose measured candidates start a stage command on a host the table shows."""

    def __init__(self, root: FakeWorkspace, table: FakeProcessTable) -> None:
        super().__init__(root, supports_parallel_candidates=True)
        self.table = table
        self.stage_of: dict[int, MarkedProcess] = {}

    async def create_measured_candidate(
        self, revision: str, *, step_tag: str | None = None
    ) -> CandidateWorkspace:
        candidate = await super().create_measured_candidate(revision, step_tag=step_tag)
        if step_tag is not None:
            self.stage_of[id(candidate)] = self.table.start_stage(
                STEP_TAG_ENV, stage_mark(RUN, step_tag)
            )
        return candidate


class HostEvaluation(FakeEvaluation):
    """An evaluation whose stage command ends when its call does, however the call ends.

    The executor's own cancel ends the command with its call. Only a host that died leaves it,
    which the tests of the reaper start in the table directly.
    """

    workspaces: HostWorkspaces

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
        output: OutputSink,
    ) -> AccuracyEvaluation:
        try:
            return await super().accuracy(workspace, reuse=reuse, output=output)
        finally:
            self._command_ended(workspace)

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
        output: OutputSink,
    ) -> BenchmarkEvaluation:
        try:
            return await super().benchmark(workspace, objectives=objectives, output=output)
        finally:
            self._command_ended(workspace)

    def _command_ended(self, workspace: Workspace) -> None:
        stage = self.workspaces.stage_of.pop(id(workspace), None)
        if stage is not None:
            self.workspaces.table.exit(stage)


class StagesHeard:
    """The host's observer: what it was told of each stage of a job."""

    def __init__(self) -> None:
        self.settled: list[StageSettled] = []

    def stage_started(self, event: StageStarted) -> None:
        del event

    def stage_settled(self, event: StageSettled) -> None:
        self.settled.append(event)


class World:
    """One host: an evaluation executor over Fakes, a reaper over its process table, the runner."""

    def __init__(self, root: Path, shows: Shows = Shows.ENVIRONMENT) -> None:
        self.clock = VirtualClock(1000.0)
        self.table = FakeProcessTable(shows)
        self.evaluation = HostEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
        self.gates = [self.evaluation.gate("benchmark", call) for call in range(GATED_BENCHMARKS)]
        self.workspaces = HostWorkspaces(FakeWorkspace(path=Path("/workspace")), self.table)
        self.evaluation.workspaces = self.workspaces
        self.executor = PollingEvaluationExecutor(
            self.evaluation, self.workspaces, clock=self.clock
        )
        self.blocking = DetachedBlockingRunner()
        self.reaper = ProcessStageWorkReaper(self.table, RUN, self.blocking)
        store = receipt_store_in(root)
        self.evidence = ReceiptEvidenceLedger(store)
        self.heard = StagesHeard()
        self.decisions = MeasurementDecisions()
        self.runner = LocalJobStepRunner(
            self.executor,
            self.reaper,
            JobBooks(
                JobRegistry(store), self.evidence, MeasurementProgress(self.heard), self.decisions
            ),
            clock=self.clock,
            poll_interval_s=POLL_S,
        )
        self.snapshot = self.run(self.workspaces.root.snapshot("candidate"))

    def run[T](self, main: Coroutine[Any, Any, T]) -> T:
        return run_virtual(self.clock, main)

    def step(self, tag: RequestId, *, ends: bool) -> Request:
        """A step that ends by itself (an accuracy stage) or runs until cancelled (a benchmark)."""
        stages = ("accuracy",) if ends else ("benchmark",)
        return submission(tag.root, override=plan(stages=stages, candidate=self.snapshot))

    async def left_running(self, tag: RequestId) -> int:
        """Evaluation work the executor still runs, or stage commands a dead process left."""
        snapshot = await self.executor.availability(ResourceRequirements())
        # An evaluation task and the stage command it started are one piece of work.
        commands = self.table.running(STEP_TAG_ENV, partial(is_stage_mark_of, RUN, tag))
        return max(snapshot.in_flight, commands)

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
    world.table.unreadable = True
    try:
        yield
    finally:
        world.table.unreadable = False


def tag_of(name: str) -> RequestId:
    return RequestId(root=name)


class LocalJobContract(StepRunnerContract):
    """The contract over the local runner, on a host whose table shows what ``shows`` shows."""

    shows = Shows.ENVIRONMENT

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def step_runner_under_test(self) -> StepRunnerUnderTest:
        world = World(self.root, self.shows)
        return StepRunnerUnderTest(
            runner=world.runner,
            run=world.run,
            finishing_step=lambda tag: world.step(tag, ends=True),
            running_step=lambda tag: world.step(tag, ends=False),
            left_running=world.left_running,
            unreachable=lambda: unreachable(world),
            blocking=world.blocking,
        )


class TestLocalJobStepRunnerOnLinux(LocalJobContract):
    shows = Shows.ENVIRONMENT


class TestLocalJobStepRunnerOnMacOS(LocalJobContract):
    shows = Shows.LEADER_ARGUMENTS


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
    ended = world.table.start_stage(STEP_TAG_ENV, stage_mark(RUN, handle_of_life(left, 0)))
    world.table.start_stage(STEP_TAG_ENV, stage_mark(RUN, handle_of_life(other, 0)))

    async def main() -> None:
        assert await world.runner.clear(world.step(left, ends=False), left) == Cleared()
        assert await world.left_running(left) == 0
        assert await world.left_running(other) == 1

    world.run(main())
    assert world.table.killed == [ended]


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


def test_a_finished_step_has_recorded_every_reading_it_reports_in_the_evidence_ledger(
    tmp_path: Path,
) -> None:
    """The operation that decodes a job's readings reads only the ledger.

    Core takes the step's report as the job's last observation, so no poll after it writes the
    readings: a report whose evidence the ledger does not hold reads back as undecodable and ends
    the run failed. A step that reuses an ended job reports its evidence under its own request.
    """
    world = World(tmp_path)
    source, reusing = tag_of("source"), tag_of("reusing")

    async def main() -> None:
        measured = world.step(source, ends=True)
        assert isinstance(measured, SubmitMeasurement)
        first = await world.runner.run(measured, source, 1, DEADLINE_AT, CancelSignal())
        step = measured.model_copy(update={"request_id": reusing, "reuses": source})
        again = await world.runner.run(step, reusing, 1, DEADLINE_AT, CancelSignal())
        for done in (first, again):
            assert isinstance(done, Finished)
            assert done.report.evidence
            for ref in done.report.evidence:
                held = world.evidence.lookup(ref.key)
                assert held is not None, ref.key
                assert held.purpose == measured.plan.purpose

    world.run(main())


def test_a_finished_step_tells_the_host_its_stages_and_names_the_decision_it_answers(
    tmp_path: Path,
) -> None:
    """The poll that used to tell the host and name the span no longer follows the report."""
    world = World(tmp_path)
    tag = tag_of("ended")
    decision = DecisionId(root="decision-1")

    async def main() -> None:
        step = world.step(tag, ends=True).model_copy(update={"decision_id": decision})
        done = await world.runner.run(step, tag, 1, DEADLINE_AT, CancelSignal())
        assert isinstance(done, Finished)

    world.run(main())
    assert [(event.stage_id, event.passed) for event in world.heard.settled] == [("accuracy", True)]
    handle = world.heard.settled[0].handle_id
    assert world.decisions.decision(handle) == decision.root


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
