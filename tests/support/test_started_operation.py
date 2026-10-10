"""The start-wait helper never parks a test on an operation that cannot start."""

from __future__ import annotations

import asyncio
import threading
from enum import StrEnum

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.started_operation import wait_until_started


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
