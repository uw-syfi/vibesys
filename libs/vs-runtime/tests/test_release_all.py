"""``release_all`` finishes every release and reports a cancelled cleanup as a cancellation (#1634).

A second interrupt cancels the run's task while it is already cleaning up. The
cleanup used to be interrupted at its current step and the cancellation was
recorded as a cleanup failure, so the run ended in ``RunCleanupError``.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api import RunCleanupError
from vs_runtime.api.infrastructure import release_all

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _Step(StrEnum):
    OK = "ok"
    FAILS = "fails"
    # The release's own work ends cancelled, as when a task it waits on was cancelled.
    ENDS_CANCELLED = "ends-cancelled"
    # The caller is cancelled while this release is running, as by a second interrupt.
    CALLER_CANCELLED = "caller-cancelled"


class _ReleaseFailedError(RuntimeError):
    pass


def _release(
    step: _Step, caller: asyncio.Task[None], finished: list[int], index: int
) -> Callable[[], Awaitable[None]]:
    async def release() -> None:
        if step is _Step.CALLER_CANCELLED:
            caller.cancel()
        # Yield, so the cancellation lands while this release is mid-way.
        await asyncio.sleep(0)
        if step is _Step.FAILS:
            raise _ReleaseFailedError
        if step is _Step.ENDS_CANCELLED:
            raise asyncio.CancelledError
        finished.append(index)

    return release


async def _run(steps: list[_Step]) -> tuple[BaseException | None, list[int]]:
    finished: list[int] = []
    outcomes: list[BaseException | None] = []

    async def caller() -> None:
        task = asyncio.current_task()
        assert task is not None
        try:
            await release_all(
                [_release(step, task, finished, index) for index, step in enumerate(steps)],
                failure="cleanup failed",
            )
        except BaseException as error:  # lint-waiver: LW-948031 [BLE001]; the test records the outcome whatever its type, cancellation included.
            outcomes.append(error)
            raise
        outcomes.append(None)

    await asyncio.gather(asyncio.create_task(caller()), return_exceptions=True)
    return outcomes[0], finished


@given(steps=st.lists(st.sampled_from(_Step), max_size=6))
def test_every_release_runs_to_its_end_and_the_outcome_is_typed(steps: list[_Step]) -> None:
    outcome, finished = asyncio.run(_run(steps))

    # No interruption stops a later release or cuts an earlier one short.
    expected_finished = [
        i for i, step in enumerate(steps) if step in {_Step.OK, _Step.CALLER_CANCELLED}
    ]
    assert finished == expected_finished
    failures = steps.count(_Step.FAILS)
    if _Step.ENDS_CANCELLED in steps or _Step.CALLER_CANCELLED in steps:
        # A cancellation is never reported as a cleanup failure; real failures are notes on it.
        assert isinstance(outcome, asyncio.CancelledError)
        assert len(getattr(outcome, "__notes__", [])) == failures
    elif failures:
        assert isinstance(outcome, RunCleanupError)
        assert len(outcome.failures) == failures
        assert all(isinstance(error, _ReleaseFailedError) for error in outcome.failures)
    else:
        assert outcome is None


def test_a_cancellation_during_a_release_does_not_interrupt_it() -> None:
    outcome, finished = asyncio.run(_run([_Step.OK, _Step.CALLER_CANCELLED, _Step.OK]))

    assert isinstance(outcome, asyncio.CancelledError)
    assert finished == [0, 1, 2]
