"""Waits that must finish survive any number of cancellations of their caller."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_async_ops.api import drain, finish, run_to_end

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _Work:
    """An owned task that ends only when released, recording how it ended."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.release = asyncio.Event()
        self.ended = False
        self._failure = failure
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> str:
        await self.release.wait()
        self.ended = True
        if self._failure is not None:
            raise self._failure
        return "done"


async def _cancel_repeatedly(caller: asyncio.Task[object], times: int) -> None:
    for _ in range(times):
        await asyncio.sleep(0)  # let the caller reach (or re-enter) its wait
        caller.cancel()
    await asyncio.sleep(0)


async def _cancelled_caller_outcome(
    wait: Callable[[_Work], Awaitable[object]], cancellations: int, *, failure: Exception | None
) -> tuple[_Work, asyncio.Task[object]]:
    work = _Work(failure=failure)
    caller = asyncio.ensure_future(wait(work))
    await _cancel_repeatedly(caller, cancellations)
    assert not caller.done(), "the caller must keep waiting for the owned task"
    assert not work.ended
    work.release.set()
    await asyncio.gather(caller, return_exceptions=True)
    return work, caller


@given(cancellations=st.integers(min_value=1, max_value=6))
def test_drain_waits_for_every_task_however_often_the_caller_is_cancelled(
    cancellations: int,
) -> None:
    async def scenario() -> None:
        work, caller = await _cancelled_caller_outcome(
            lambda w: drain(w.task), cancellations, failure=None
        )
        assert work.ended
        assert caller.cancelled()

    asyncio.run(scenario())


@given(cancellations=st.integers(min_value=1, max_value=6), fails=st.booleans())
def test_finish_lets_the_task_end_then_re_raises_the_callers_cancellation(
    cancellations: int, *, fails: bool
) -> None:
    async def scenario() -> None:
        failure = RuntimeError("boom") if fails else None
        work, caller = await _cancelled_caller_outcome(
            lambda w: finish(w.task), cancellations, failure=failure
        )
        assert work.ended
        assert caller.cancelled()
        assert work.task.done()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_finish_notes_the_failure_of_the_task_the_cancellation_waited_for() -> None:
    work = _Work(failure=RuntimeError("boom"))
    caller = asyncio.ensure_future(finish(work.task))
    await _cancel_repeatedly(caller, 2)
    work.release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert any("boom" in note for note in raised.value.__notes__)


@pytest.mark.asyncio
async def test_finish_returns_the_result_and_raises_the_failure_when_not_cancelled() -> None:
    ok = _Work()
    ok.release.set()
    assert await finish(ok.task) == "done"

    bad = _Work(failure=RuntimeError("boom"))
    bad.release.set()
    with pytest.raises(RuntimeError, match="boom"):
        await finish(bad.task)


@pytest.mark.asyncio
async def test_a_task_that_was_itself_cancelled_raises_cancelled_error() -> None:
    work = _Work()
    caller = asyncio.ensure_future(finish(work.task))
    await asyncio.sleep(0)
    work.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller


@given(cancellations=st.integers(min_value=1, max_value=6))
def test_run_to_end_returns_the_result_however_often_the_caller_is_cancelled(
    cancellations: int,
) -> None:
    async def scenario() -> None:
        work = _Work()
        caller = asyncio.ensure_future(run_to_end(work.task))
        await _cancel_repeatedly(caller, cancellations)
        assert not caller.done()
        work.release.set()
        assert await caller == "done"

    asyncio.run(scenario())
