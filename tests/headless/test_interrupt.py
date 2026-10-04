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
from vibesys.api import (
    AgentOutputChunkData,
    CoreEvent,
    CoreEventType,
    RunResult,
    RunSession,
    RunStatus,
    RunStopped,
)
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

    async def await_result(self) -> RunResult | None:
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

    async def await_result(self) -> RunResult:
        self.started.set()
        signal.raise_signal(signal.SIGINT)
        await self.stop_requested.wait()
        return RunResult(run_id="signalled", loop="test", succeeded=False, status=RunStatus.STOPPED)


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
            result = await supervise(handle)
            assert result.status is RunStatus.STOPPED

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


class _CleanupSession(_InterruptibleSession):
    """Execution whose asynchronous cleanup is held until explicitly released."""

    def __init__(self) -> None:
        super().__init__()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_entries = 0
        self.cleanup_completions = 0
        self.close_calls = 0

    async def await_result(self) -> None:
        self.started.set()
        try:
            await self.stop_requested.wait()
        finally:
            self.cleanup_entries += 1
            self.cleanup_entered.set()
            await self.cleanup_release.wait()
            self.cleanup_completions += 1

    def close(self) -> None:
        super().close()
        self.close_calls += 1


class _BrokenPipeOutput(StringIO):
    """A terminal output whose reader has closed its pipe."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(text)


class _CancellationBarrierHandle(FakeRunHandle[CoreEvent, RunResult, RunSession]):
    """Faithful handle that acknowledges repeated cancellation requests."""

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.cancel_calls = 0
        self.repeated_cancel = asyncio.Event()

    def cancel(self) -> None:
        super().cancel()
        self.cancel_calls += 1
        if self.cancel_calls == 2:
            self.repeated_cancel.set()


@pytest.mark.asyncio
async def test_broken_pipe_during_cancellation_does_not_interrupt_async_cleanup() -> None:
    session = _CleanupSession()
    handle = _CancellationBarrierHandle("cleanup-render-failed")
    handle.bind(cast("RunSession", session))
    handle.start()
    supervision = asyncio.create_task(
        supervise(
            handle,
            run(handle, renderer=HeadlessRenderer(out=_BrokenPipeOutput())),
            handle_signals=False,
        )
    )
    await session.started.wait()
    handle.cancel()
    await session.cleanup_entered.wait()

    handle.publish(
        CoreEvent(
            timestamp=datetime(2000, 1, 1, tzinfo=UTC),
            type=CoreEventType.AGENT_OUTPUT_CHUNK,
            data=AgentOutputChunkData(channel="assistant", content="render during cleanup"),
        )
    )
    await handle.repeated_cancel.wait()
    session.cleanup_release.set()

    with pytest.raises(BrokenPipeError):
        await supervision
    assert session.cleanup_entries == 1
    assert session.cleanup_completions == 1
    assert session.close_calls == 1
    assert session.closed
    with pytest.raises(asyncio.CancelledError):
        await handle.result()
