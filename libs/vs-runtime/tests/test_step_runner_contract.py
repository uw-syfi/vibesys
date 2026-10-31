"""Every ``StepRunner`` passes the contract, and the Fake is no more forgiving than a real one."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

import vs_core.api as core
from vs_core.api.recovery import Cancelled, CancelReason, StepResult
from vs_runtime.api.step_runner import Adopted, CancelSignal, Cleared, ClearUnavailableError
from vs_runtime.api.testing import (
    AttemptHangs,
    AttemptRaises,
    AttemptReturns,
    FakeStepRunner,
    LeftoverWorkError,
    RunnerMethod,
    StepRunnerContract,
    StepRunnerUnderTest,
    finished_result,
)
from vs_sim.api.testing import VirtualClock, run_virtual, yield_until

_SLOW = "slow"
DEADLINE_AT = 1_000.0


def a_step(tag: core.RequestId, resource: str = "quick") -> core.Request:
    return core.ObserveOwnedJob(
        request_id=tag,
        scope=core.Scope(owner=core.RunId(root="run"), generation=0),
        resource_id=core.ResourceId(root=resource),
    )


def tag_named(name: str) -> core.RequestId:
    return core.RequestId(root=name)


def under_test(*, adopts: bool) -> StepRunnerUnderTest:
    clock = VirtualClock()

    def behave(
        step: core.Request, _tag: core.RequestId, _attempt: int
    ) -> AttemptHangs | AttemptReturns:
        slow = isinstance(step, core.ObserveOwnedJob) and step.resource_id.root == _SLOW
        return AttemptHangs() if slow else AttemptReturns(after_s=1.0)

    runner = FakeStepRunner(clock, adopts=adopts, behave=behave)

    def running_step(tag: core.RequestId) -> core.Request:
        return a_step(tag, _SLOW)

    async def left_running(tag: core.RequestId) -> int:
        return int(tag in runner.running())

    return StepRunnerUnderTest(
        runner=runner,
        run=lambda main: run_virtual(clock, main),
        finishing_step=a_step,
        running_step=running_step,
        left_running=left_running,
        unreachable=runner.unreachable,
    )


class TestFakeStepRunner(StepRunnerContract):
    def step_runner_under_test(self) -> StepRunnerUnderTest:
        return under_test(adopts=False)


class TestFakeAdoptingStepRunner(StepRunnerContract):
    def step_runner_under_test(self) -> StepRunnerUnderTest:
        return under_test(adopts=True)


async def work_exists(runner: FakeStepRunner, tag: core.RequestId) -> None:
    async def observe() -> tuple[core.RequestId, ...]:
        return runner.running()

    await yield_until(observe, lambda running: tag in running, f"the work of {tag}")


def test_work_outlives_a_cancelled_task_and_a_second_attempt_without_clear_is_refused() -> None:
    clock = VirtualClock()
    runner = FakeStepRunner(clock)
    tag = tag_named("a")
    runner.script(tag, AttemptHangs(), AttemptReturns())

    async def main() -> None:
        task = asyncio.ensure_future(runner.run(a_step(tag), tag, 1, DEADLINE_AT, CancelSignal()))
        await work_exists(runner, tag)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert runner.running() == (tag,)
        with pytest.raises(LeftoverWorkError):
            await runner.run(a_step(tag), tag, 2, DEADLINE_AT, CancelSignal())
        assert runner.violations
        assert await runner.clear(a_step(tag), tag) == Cleared()
        assert runner.running() == ()
        assert await runner.run(
            a_step(tag), tag, 2, DEADLINE_AT, CancelSignal()
        ) == finished_result(tag)

    run_virtual(clock, main())


def test_an_adopting_runner_hands_back_the_result_of_work_that_ended_while_unattended() -> None:
    clock = VirtualClock()
    runner = FakeStepRunner(clock, adopts=True)
    tag = tag_named("a")
    runner.script(tag, AttemptReturns(after_s=5.0))

    async def main() -> None:
        task = asyncio.ensure_future(runner.run(a_step(tag), tag, 1, DEADLINE_AT, CancelSignal()))
        await work_exists(runner, tag)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await runner.clear(a_step(tag), tag) == Adopted(None)
        await clock.sleep(5.0)
        assert await runner.clear(a_step(tag), tag) == Adopted(finished_result(tag))
        assert runner.left_behind() == ()

    run_virtual(clock, main())


def test_a_raising_attempt_leaves_its_work_and_clear_ends_it() -> None:
    clock = VirtualClock()
    runner = FakeStepRunner(clock)
    tag = tag_named("a")
    runner.script(tag, AttemptRaises(OSError("lost the host")))

    async def main() -> None:
        with pytest.raises(OSError, match="lost the host"):
            await runner.run(a_step(tag), tag, 1, DEADLINE_AT, CancelSignal())
        assert runner.running() == (tag,)
        assert await runner.clear(a_step(tag), tag) == Cleared()
        assert runner.running() == ()

    run_virtual(clock, main())


def test_clear_while_an_attempt_is_awaited_is_a_violation() -> None:
    clock = VirtualClock()
    runner = FakeStepRunner(clock)
    tag = tag_named("a")
    runner.script(tag, AttemptHangs())

    async def main() -> None:
        cancel = CancelSignal()
        task = asyncio.ensure_future(runner.run(a_step(tag), tag, 1, DEADLINE_AT, cancel))
        await work_exists(runner, tag)
        await runner.clear(a_step(tag), tag)
        assert runner.violations
        cancel.set(CancelReason.PAUSE_NOW)
        assert await task == Cancelled(reason=CancelReason.PAUSE_NOW)

    run_virtual(clock, main())


def test_clear_unavailable_n_times_then_answers_and_every_call_is_in_the_ledger() -> None:
    clock = VirtualClock()
    runner = FakeStepRunner(clock)
    tag = tag_named("a")
    runner.clears_unavailable(2)

    async def main() -> None:
        for _ in range(2):
            with pytest.raises(ClearUnavailableError) as refused:
                await runner.clear(a_step(tag), tag)
            assert refused.value.tag == tag
        assert await runner.clear(a_step(tag), tag) == Cleared()

    run_virtual(clock, main())
    assert [call.kind for call in runner.calls] == [RunnerMethod.CLEAR] * 3


class StuckRunner:
    """A defective runner: ``clear`` leaves the work running, and the next run waits on it.

    Like an evaluation executor whose admission lock the leftover job still holds, the wait
    never ends and the clock never stops advancing.
    """

    def __init__(self, clock: VirtualClock) -> None:
        self._clock = clock
        self.held: set[core.RequestId] = set()

    async def run(
        self,
        step: core.Request,
        tag: core.RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        del attempt, deadline_at
        while self.held:
            await self._clock.sleep(1.0)
        self.held.add(tag)
        if isinstance(step, core.ObserveOwnedJob) and step.resource_id.root == _SLOW:
            reason = await cancel.wait()
            self.held.discard(tag)
            return Cancelled(reason=reason)
        await self._clock.sleep(1.0)
        self.held.discard(tag)
        return finished_result(tag)

    async def clear(self, step: core.Request, tag: core.RequestId) -> Adopted | Cleared:
        del step, tag
        return Cleared()


class StuckContract(StepRunnerContract):
    def step_runner_under_test(self) -> StepRunnerUnderTest:
        clock = VirtualClock()
        runner = StuckRunner(clock)

        async def left_running(tag: core.RequestId) -> int:
            return int(tag in runner.held)

        return StepRunnerUnderTest(
            runner=runner,
            run=lambda main: run_virtual(clock, main),
            finishing_step=a_step,
            running_step=lambda tag: a_step(tag, _SLOW),
            left_running=left_running,
            unreachable=contextlib.nullcontext,
        )


def test_a_runner_whose_clear_leaves_work_behind_fails_the_suite_instead_of_hanging_it() -> None:
    contract = StuckContract()
    with pytest.raises(AssertionError, match="did not return"):
        contract.test_after_a_kill_and_a_clear_the_same_tag_starts_clean_at_a_higher_attempt()
    with pytest.raises(AssertionError, match="did not return"):
        contract.test_any_order_of_kills_cancels_and_clears_never_runs_a_tag_twice_at_once()
