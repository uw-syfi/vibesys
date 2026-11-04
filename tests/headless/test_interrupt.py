"""Headless interrupt handling through the public run-session contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from io import StringIO
from typing import cast

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

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
    StopMode,
)
from vibesys.api.testing import FakeRunHandle
from vs_sim.api import Signals
from vs_sim.api.testing import FakeSignalSource, arrival


class _InterruptibleSession:
    """Faithful session double whose stop request lands in its active run."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.stops: list[StopMode] = []
        self.closed = False

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def await_result(self) -> RunResult | None:
        self.started.set()
        await self.stop_requested.wait()
        raise RunStopped

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        self.stops.append(mode)
        self.stop_requested.set()


@pytest.mark.asyncio
async def test_one_interrupt_stops_and_drains_the_run_before_headless_exits() -> None:
    session = _InterruptibleSession()
    handle = FakeRunHandle("interrupt")
    handle.bind(cast("RunSession", session))
    handle.start()
    headless = asyncio.create_task(run(handle))
    await arrival(session.started.wait(), headless)

    headless.cancel()

    with pytest.raises(asyncio.CancelledError):
        await headless
    assert session.stops == [StopMode.DRAIN]
    assert session.stop_requested.is_set()


class _SignalledSession(_InterruptibleSession):
    """Session whose run is interrupted by a delivered SIGINT once it is running."""

    def __init__(self, signals: FakeSignalSource) -> None:
        super().__init__()
        self._signals = signals

    async def await_result(self) -> RunResult:
        self.started.set()
        assert self._signals.deliver(Signals.SIGINT), "supervise installed no SIGINT handler"
        await self.stop_requested.wait()
        return RunResult(run_id="signalled", loop="test", succeeded=False, status=RunStatus.STOPPED)


@pytest.mark.regression
@pytest.mark.asyncio
async def test_ctrl_c_stops_the_run_cancelling_its_work_and_returns_its_stopped_result() -> None:
    """r3: Ctrl-C asked for a draining stop, so in-flight evaluation jobs ran on until the
    host's stop bound cancelled the run; the stop now cancels them itself."""
    signals = FakeSignalSource()
    session = _SignalledSession(signals)
    handle = FakeRunHandle("signalled")
    handle.bind(cast("RunSession", session))
    handle.start()

    result = await supervise(handle, signals=signals)

    assert result.status is RunStatus.STOPPED
    assert session.stops == [StopMode.CANCEL]
    assert not signals.handles(Signals.SIGINT)


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
        await supervise(handle, run(handle, renderer=HeadlessRenderer(out=output)), signals=None)

    assert session.started.is_set()
    assert session.closed
    assert session.stops == [StopMode.DRAIN]
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
            signals=None,
        )
    )
    await arrival(session.started.wait(), supervision)
    handle.cancel()
    await arrival(session.cleanup_entered.wait(), supervision)

    handle.publish(
        CoreEvent(
            timestamp=datetime(2000, 1, 1, tzinfo=UTC),
            type=CoreEventType.AGENT_OUTPUT_CHUNK,
            data=AgentOutputChunkData(channel="assistant", content="render during cleanup"),
        )
    )
    await arrival(handle.repeated_cancel.wait(), supervision)
    session.cleanup_release.set()

    with pytest.raises(BrokenPipeError):
        await supervision
    assert session.cleanup_entries == 1
    assert session.cleanup_completions == 1
    assert session.close_calls == 1
    assert session.closed
    with pytest.raises(asyncio.CancelledError):
        await handle.result()


class _EndingSession(_InterruptibleSession):
    """A run that receives the given signals once running, and notes how it was told to end."""

    def __init__(self, signals: FakeSignalSource, delivered: list[Signals]) -> None:
        super().__init__()
        self._signals = signals
        self._delivered = delivered
        self.cancelled = False

    async def await_result(self) -> RunResult:
        self.started.set()
        for number in self._delivered:
            assert self._signals.deliver(number), f"supervise installed no {number.name} handler"
        try:
            await self.stop_requested.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return RunResult(run_id="ending", loop="test", succeeded=False, status=RunStatus.STOPPED)


_SHUTDOWN = (Signals.SIGINT, Signals.SIGTERM, Signals.SIGHUP)


@pytest.mark.regression
@given(st.lists(st.sampled_from(_SHUTDOWN), min_size=1, max_size=5))
@example([Signals.SIGINT])
@example([Signals.SIGTERM])
@pytest.mark.asyncio
async def test_every_shutdown_signal_ends_the_run_cancelling_its_work_never_draining_it(
    delivered: list[Signals],
) -> None:
    """SIGINT and SIGTERM end a run the same way: its work in flight is cancelled, not drained.

    Ctrl-C asks for a stop in ``StopMode.CANCEL``; SIGTERM and SIGHUP cancel the run's
    task. Either way the run's core cancels its evaluations and the teardown closes the
    executors, which cancel every job (``test_slurm_shutdown_leaves_no_job``).
    """
    signals = FakeSignalSource()
    session = _EndingSession(signals, delivered)
    handle = FakeRunHandle("ending")
    handle.bind(cast("RunSession", session))
    handle.start()

    if set(delivered) == {Signals.SIGINT}:
        result = await supervise(handle, signals=signals)
        assert result.status is RunStatus.STOPPED
    else:
        with pytest.raises(SystemExit) as exited:
            await supervise(handle, signals=signals)
        assert exited.value.code == 128 + delivered[0]

    assert all(mode is StopMode.CANCEL for mode in session.stops)
    assert session.stops or session.cancelled
    assert not any(signals.handles(number) for number in _SHUTDOWN)


class _EscalatingSession(_InterruptibleSession):
    """A run that ends only once released, noting each stop's mode."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def await_result(self) -> RunResult:
        self.started.set()
        await self.release.wait()
        return RunResult(run_id="escalated", loop="test", succeeded=False, status=RunStatus.STOPPED)


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_ctrl_c_after_a_draining_stop_escalates_it_to_cancel_the_work() -> None:
    """periodic-1330 MED-2: a Ctrl-C while the run drained changed nothing.

    Cancelling the supervision drains the run; a Ctrl-C then still asks the run to cancel
    its work in flight, as Ctrl-C always does, instead of being dropped as a repeat.
    """
    signals = FakeSignalSource()
    session = _EscalatingSession()
    handle = FakeRunHandle("escalated")
    handle.bind(cast("RunSession", session))
    handle.start()
    supervision = asyncio.create_task(supervise(handle, signals=signals))
    await arrival(session.started.wait(), supervision)

    supervision.cancel()
    await arrival(session.stop_requested.wait(), supervision)
    assert signals.deliver(Signals.SIGINT)
    modes = list(session.stops)
    session.release.set()

    with pytest.raises(asyncio.CancelledError):
        await supervision
    assert modes == [StopMode.DRAIN, StopMode.CANCEL]
