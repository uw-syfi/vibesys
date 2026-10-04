"""Headless interrupt handling through the public run-session contract."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from io import StringIO
from typing import cast

import pytest

from entrypoints.run import supervise
from headless import HeadlessRenderer, run
from vibesys.api import AgentOutputChunkData, CoreEvent, CoreEventType, RunSession, RunStopped
from vibesys.api.testing import FakeRunHandle


class _InterruptibleSession:
    """Faithful session double whose stop request lands in its active run."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.stop_calls = 0
        self.closed = False

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

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
    handle = FakeRunHandle("interrupt")
    handle.bind(cast("RunSession", session))
    handle.start()
    headless = asyncio.create_task(run(handle))
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

        async def execute() -> None:
            handle = FakeRunHandle("signalled")
            handle.bind(cast("RunSession", session))
            handle.start()
            await supervise(handle)

        with pytest.raises(KeyboardInterrupt):
            asyncio.run(execute())
        assert session.stop_calls == 1
        assert signal.getsignal(signal.SIGINT) is _replacement_handler
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.asyncio
async def test_render_failure_drains_the_owned_run_before_process_scope_exits() -> None:
    session = _InterruptibleSession()
    handle = FakeRunHandle("render-failed")
    handle.bind(cast("RunSession", session))
    handle.start()
    handle.publish(
        CoreEvent(
            timestamp=datetime(2000, 1, 1, tzinfo=UTC),
            type=CoreEventType.AGENT_OUTPUT_CHUNK,
            data=AgentOutputChunkData(channel="assistant", content="render this"),
        )
    )
    output = StringIO()
    output.close()

    with pytest.raises(ValueError, match="closed file"):
        await supervise(
            handle, run(handle, renderer=HeadlessRenderer(out=output)), handle_signals=False
        )

    assert session.started.is_set()
    assert session.closed
    assert session.stop_calls == 1
    with pytest.raises(asyncio.CancelledError):
        await handle.result()
