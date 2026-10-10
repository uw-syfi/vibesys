"""The start-wait helper never parks a test on an operation that cannot start."""

from __future__ import annotations

import asyncio
import threading
from enum import StrEnum

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.started_operation import (
    arrival,
    wait_until_executor_started,
    wait_until_started,
)

from vs_evaluation.api import EvaluationState
from vs_evaluation.api.testing import FakeClock, FakeEvaluationExecutor


class _Ending(StrEnum):
    RETURNS = "returns"
    RAISES = "raises"
    STARTS_THEN_HOLDS = "starts_then_holds"


class _FailureError(Exception):
    pass


def _run(ending: _Ending, *, yields: int) -> None:
    started = threading.Event()
    release = threading.Event()

    def held() -> None:
        started.set()
        release.wait()

    async def operation() -> None:
        for _ in range(yields):
            await asyncio.sleep(0)
        if ending is _Ending.RAISES:
            raise _FailureError
        if ending is _Ending.STARTS_THEN_HOLDS:
            await asyncio.to_thread(held)

    async def scenario() -> None:
        task = asyncio.ensure_future(operation())
        try:
            await wait_until_started(started, task)
        finally:
            release.set()
            if not task.done():
                task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@given(ending=st.sampled_from(_Ending), yields=st.integers(min_value=0, max_value=5))
def test_the_wait_ends_with_the_operations_own_outcome_at_any_point(
    ending: _Ending, yields: int
) -> None:
    if ending is _Ending.STARTS_THEN_HOLDS:
        _run(ending, yields=yields)
    elif ending is _Ending.RAISES:
        with pytest.raises(_FailureError):
            _run(ending, yields=yields)
    else:
        with pytest.raises(AssertionError, match="without reaching its held step"):
            _run(ending, yields=yields)


def test_an_asyncio_event_wait_ends_when_the_operation_ends_first() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def operation() -> None:
            raise _FailureError

        task = asyncio.ensure_future(operation())
        with pytest.raises(_FailureError):
            await wait_until_started(started, task)

    asyncio.run(scenario())


_ENDED = (
    EvaluationState.SUCCEEDED,
    EvaluationState.FAILED,
    EvaluationState.CANCELED,
    EvaluationState.SUPERSEDED,
)
_LIVE = (
    EvaluationState.QUEUED,
    EvaluationState.STARTING,
    EvaluationState.RUNNING,
    EvaluationState.CANCELING,
)


def _executor_run(state: EvaluationState, *, yields: int, starts: bool) -> None:
    async def scenario() -> None:
        executor = FakeEvaluationExecutor(FakeClock(), advance_clock_on_timeout=False)
        started = threading.Event()
        if starts:
            started.set()

        async def evaluation_moves_on() -> None:
            for _ in range(yields):
                await asyncio.sleep(0)
            executor.set_state(
                "h", state, failure="failed" if state is EvaluationState.FAILED else None
            )

        mover = asyncio.ensure_future(evaluation_moves_on())
        try:
            await wait_until_executor_started(started, executor, "h")
        finally:
            await mover

    asyncio.run(scenario())


@given(state=st.sampled_from(_ENDED), yields=st.integers(min_value=0, max_value=5))
def test_an_evaluation_that_ends_before_its_step_starts_fails_the_wait(
    state: EvaluationState, yields: int
) -> None:
    with pytest.raises(AssertionError, match="without reaching its held step"):
        _executor_run(state, yields=yields, starts=False)


@given(state=st.sampled_from(_LIVE), yields=st.integers(min_value=0, max_value=5))
def test_a_started_step_returns_while_the_evaluation_stays_live(
    state: EvaluationState, yields: int
) -> None:
    _executor_run(state, yields=yields, starts=True)


@given(
    ending=st.sampled_from(_Ending),
    yields=st.integers(min_value=0, max_value=5),
    queue=st.booleans(),
)
def test_an_arrival_wait_ends_with_the_producers_outcome(
    ending: _Ending, yields: int, *, queue: bool
) -> None:
    async def scenario() -> object:
        event = asyncio.Event()
        items: asyncio.Queue[int] = asyncio.Queue()
        hold = asyncio.Event()

        async def producer() -> None:
            for _ in range(yields):
                await asyncio.sleep(0)
            if ending is _Ending.RAISES:
                raise _FailureError
            if ending is _Ending.STARTS_THEN_HOLDS:
                items.put_nowait(7)
                event.set()
                await hold.wait()

        task = asyncio.ensure_future(producer())
        try:
            return await arrival(items.get() if queue else event.wait(), task)
        finally:
            hold.set()
            await asyncio.gather(task, return_exceptions=True)

    if ending is _Ending.STARTS_THEN_HOLDS:
        assert asyncio.run(scenario()) == (7 if queue else True)
    elif ending is _Ending.RAISES:
        with pytest.raises(_FailureError):
            asyncio.run(scenario())
    else:
        with pytest.raises(AssertionError, match="without the arrival"):
            asyncio.run(scenario())


def test_an_arrival_that_is_already_there_wins_over_a_finished_producer() -> None:
    async def scenario() -> None:
        event = asyncio.Event()
        event.set()

        async def producer() -> None:
            return None

        task = asyncio.ensure_future(producer())
        await task
        await arrival(event.wait(), task)

    asyncio.run(scenario())
