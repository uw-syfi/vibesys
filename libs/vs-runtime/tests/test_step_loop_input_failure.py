"""An input that fails stops the loop with that failure, never a loop that carries on or spins.

The loop waits on its flights and on the log's next input at once. The input is a task the loop
owns, so a failure of it is the loop's failure: core refusing an input it was handed (a defect, or
a record the fold rejects) must come out of ``run_step_loop`` as itself. A loop that dropped the
failure asked for the next input again, which failed again at once with nothing to wait on, so the
host spun without ever returning or advancing its clock (found by a world whose ``clear`` fails
for good, with the run's deadline drain landing on a step the dead host left half done).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_runtime.api.step_runner import StallBoard, StepLoopTiming, run_step_loop
from vs_runtime.api.testing import (
    AttemptHangs,
    FakeRunRecords,
    FakeStepLog,
    FakeStepRunner,
    fake_run_seed,
)
from vs_sim.api.testing import VirtualClock, examples, loop_threads, run_virtual

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request, RequestId
    from vs_core.api.recovery import (
        End,
        Interruption,
        OpenStep,
        RecordRefusal,
        Run,
        StepResult,
    )
    from vs_runtime.api.step_runner import StepRecord
    from vs_runtime.api.testing import Behavior

TIMING = StepLoopTiming(
    cancel_grace_s=2.0, clear_timeout_s=3.0, clear_backoff_s=1.0, clear_backoff_max_s=4.0
)
BOUND_S = 3600.0
"""Virtual seconds the loop gets to fail; it only guards against a loop that never does."""


class InputRefusedError(Exception):
    """What the input of the test's log fails with."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("core refused the input the log was handed")


class FailingInputLog:
    """The log of a run whose next input fails once, after waiting, and is never asked again.

    A call after the failure waits forever: a loop that dropped the failure and asked again waits
    there instead of failing, which the test's bound then names, rather than spinning.
    """

    def __init__(self, inner: FakeStepLog, clock: VirtualClock, *, after: float) -> None:
        self._inner = inner
        self._clock = clock
        self._after = after
        self.asked = 0

    @property
    def state(self) -> CoreState:
        return self._inner.state

    def open_step(self, tag: RequestId) -> OpenStep | None:
        return self._inner.open_step(tag)

    async def step_started(self, run: Run) -> Run | RecordRefusal:
        return await self._inner.step_started(run)

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> StepRecord:
        return await self._inner.step_finished(tag, attempt, result)

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        await self._inner.attempt_interrupted(tag, attempt, why)

    async def clock_advanced(self, at: float) -> None:
        await self._inner.clock_advanced(at)

    async def run_ended(self, end: End) -> None:
        await self._inner.run_ended(end)

    async def next_input(self) -> None:
        self.asked += 1
        if self.asked > 1:
            await asyncio.Event().wait()
        await self._clock.sleep(self._after)
        raise InputRefusedError

    def stalled(self) -> str | None:
        return self._inner.stalled()


def holds(_step: Request, _tag: RequestId, _attempt: int) -> Behavior:
    return AttemptHangs()


@pytest.mark.regression
@settings(max_examples=examples(pr=8, nightly=60))
@given(after=st.floats(min_value=0.0, max_value=500.0, allow_nan=False))
def test_an_input_that_fails_ends_the_loop_with_its_failure_whenever_it_fails(after: float) -> None:
    """The failure of the loop's input, at any time while a step is in flight, is what the loop raises."""
    clock = VirtualClock()

    async def main() -> tuple[type[BaseException] | None, int]:
        seed = fake_run_seed([1000.0], 3)
        records = FakeRunRecords()
        inner = FakeStepLog(records, seed.state, seed.registry, clock, loop_threads())
        log = FailingInputLog(inner, clock, after=after)
        runner = FakeStepRunner(clock, adopts=True, behave=holds)
        loop = asyncio.ensure_future(run_step_loop(log, runner, clock, TIMING, StallBoard()))
        timer = asyncio.ensure_future(clock.sleep(BOUND_S))
        await asyncio.wait({loop, timer}, return_when=asyncio.FIRST_COMPLETED)
        timer.cancel()
        if not loop.done():
            loop.cancel()
        await asyncio.gather(loop, timer, return_exceptions=True)
        failed = None if loop.cancelled() else loop.exception()
        return (None if failed is None else type(failed)), log.asked

    failed, asked = run_virtual(clock, main())
    assert failed is InputRefusedError
    assert asked == 1
