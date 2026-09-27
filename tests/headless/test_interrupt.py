"""Headless interrupt handling through the public run-session contract."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from headless.execute import _await_interruptibly
from vibesys.api import RunSession, RunStopped


class _InterruptibleSession:
    """Faithful session double whose stop request lands in its active run."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.stop_calls = 0

    async def await_result(self) -> None:
        self.started.set()
        await self.stop_requested.wait()
        raise RunStopped

    def stop(self) -> None:
        self.stop_calls += 1
        self.stop_requested.set()


@pytest.mark.asyncio
async def test_one_interrupt_stops_and_drains_the_run_before_headless_exits() -> None:
    session = _InterruptibleSession()
    headless = asyncio.create_task(_await_interruptibly(cast("RunSession", session)))
    await session.started.wait()

    headless.cancel()

    with pytest.raises(asyncio.CancelledError):
        await headless
    assert session.stop_calls == 1
    assert session.stop_requested.is_set()
