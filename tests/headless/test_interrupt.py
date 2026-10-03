"""Headless interrupt handling through the public run-session contract."""

from __future__ import annotations

import asyncio
import signal
from typing import cast

import pytest

from headless.execute import _await_interruptibly, _run_interruptibly
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


class _SignalledSession(_InterruptibleSession):
    """Session whose run receives a real SIGINT once it is running."""

    async def await_result(self) -> None:
        self.started.set()
        signal.raise_signal(signal.SIGINT)
        await self.stop_requested.wait()
        raise RunStopped


def _replacement_handler(signum: int, frame: object) -> None:
    """Stand-in for an import-time SIGINT handler that raises directly."""
    del signum, frame
    raise KeyboardInterrupt


def test_ctrl_c_drains_the_run_even_when_a_dependency_replaced_the_sigint_handler() -> None:
    """r6: the Docker backend's import-time handler made Ctrl-C exit without a stop."""
    previous = signal.signal(signal.SIGINT, _replacement_handler)
    try:
        session = _SignalledSession()
        with pytest.raises(KeyboardInterrupt):
            _run_interruptibly(cast("RunSession", session))
        assert session.stop_calls == 1
        assert signal.getsignal(signal.SIGINT) is _replacement_handler
    finally:
        signal.signal(signal.SIGINT, previous)
