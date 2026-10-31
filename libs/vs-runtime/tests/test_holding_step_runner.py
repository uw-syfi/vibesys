"""A ``HoldingStepRunner`` stops exactly the steps of the held kinds at the window it names."""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_runtime.api.step_runner import CancelSignal
from vs_runtime.api.testing import (
    AttemptReturns,
    FakeStepRunner,
    HoldAt,
    HoldingStepRunner,
    RunnerMethod,
    StepRunnerContract,
    StepRunnerUnderTest,
    finished_result,
)
from vs_sim.api.testing import VirtualClock, examples, run_virtual

DEADLINE_AT = 1_000.0
HELD = frozenset({core.ObserveOwnedJob})


def observe(tag: core.RequestId) -> core.Request:
    return core.ObserveOwnedJob(
        request_id=tag,
        scope=core.Scope(owner=core.RunId(root="run"), generation=0),
        resource_id=core.ResourceId(root="job"),
    )


def collect(tag: core.RequestId) -> core.Request:
    return core.CollectEvidence(
        request_id=tag,
        scope=core.Scope(owner=core.RunId(root="run"), generation=0),
        resource_id=core.ResourceId(root="job"),
    )


async def no_hold(_step: core.Request, _tag: core.RequestId, _attempt: int) -> None:
    return


def under_test(where: HoldAt) -> StepRunnerUnderTest:
    clock = VirtualClock()
    runner = FakeStepRunner(clock, behave=lambda *_: AttemptReturns(after_s=1.0))
    holding = HoldingStepRunner(runner, HELD, where, no_hold)

    async def left_running(tag: core.RequestId) -> int:
        return int(tag in runner.running())

    return StepRunnerUnderTest(
        runner=holding,
        run=lambda main: run_virtual(clock, main),
        finishing_step=observe,
        running_step=observe,
        left_running=left_running,
        unreachable=runner.unreachable,
    )


class TestHoldingStepRunnerBefore(StepRunnerContract):
    def step_runner_under_test(self) -> StepRunnerUnderTest:
        return under_test(HoldAt.BEFORE)


class TestHoldingStepRunnerAfterEffect(StepRunnerContract):
    def step_runner_under_test(self) -> StepRunnerUnderTest:
        return under_test(HoldAt.AFTER_EFFECT)


@settings(max_examples=examples(pr=6, nightly=40), deadline=None)
@given(where=st.sampled_from(HoldAt), held=st.booleans(), attempt=st.integers(1, 4))
def test_a_step_is_held_at_its_window_only_when_its_kind_is_held(
    where: HoldAt, held: bool, attempt: int
) -> None:
    clock = VirtualClock()
    runner = FakeStepRunner(clock)
    tag = core.RequestId(root="a")
    held_at: list[tuple[int, int]] = []

    async def hold(_step: core.Request, held_tag: core.RequestId, held_attempt: int) -> None:
        assert held_tag == tag
        runs = sum(call.kind is RunnerMethod.RUN for call in runner.calls)
        held_at.append((held_attempt, runs))

    holding = HoldingStepRunner(runner, HELD, where, hold)
    step = observe(tag) if held else collect(tag)

    async def main() -> None:
        for earlier in range(1, attempt):
            await holding.clear(step, tag)
            await holding.run(step, tag, earlier, DEADLINE_AT, CancelSignal())
        await holding.clear(step, tag)
        result = await holding.run(step, tag, attempt, DEADLINE_AT, CancelSignal())
        assert result == finished_result(tag)

    run_virtual(clock, main())
    if not held:
        assert held_at == []
        return
    # The attempt before the hold has run nothing of this step; the one after has run it.
    expected_runs = attempt - 1 if where is HoldAt.BEFORE else attempt
    assert held_at[-1] == (attempt, expected_runs)
    assert len(held_at) == attempt


def test_a_step_held_after_its_effect_has_no_result_until_the_hold_returns() -> None:
    clock = VirtualClock()
    runner = FakeStepRunner(clock)
    tag = core.RequestId(root="a")
    reached = asyncio.Event
    released: list[asyncio.Event] = []
    arrivals: list[asyncio.Event] = []

    async def hold(_step: core.Request, _tag: core.RequestId, _attempt: int) -> None:
        arrived, release = reached(), reached()
        arrivals.append(arrived)
        released.append(release)
        arrived.set()
        await release.wait()

    holding = HoldingStepRunner(runner, HELD, HoldAt.AFTER_EFFECT, hold)

    async def main() -> None:
        task = asyncio.ensure_future(holding.run(observe(tag), tag, 1, DEADLINE_AT, CancelSignal()))
        while not arrivals:
            await clock.sleep(0.5)
        await arrivals[0].wait()
        assert not task.done()
        assert [call.kind for call in runner.calls] == [RunnerMethod.RUN]
        released[0].set()
        assert await task == finished_result(tag)

    run_virtual(clock, main())
