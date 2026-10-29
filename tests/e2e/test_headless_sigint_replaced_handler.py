"""A real SIGINT reaches the headless supervisor even when a dependency replaced the handler.

r6: the Docker backend's import-time handler made Ctrl-C exit without a stop. The unit tests
in ``tests/headless`` deliver signals through a Fake ``SignalSource``; what only a real signal
shows is that the loop's handler wins over a handler installed with ``signal.signal``.
The scenario runs in a forked child, so no signal reaches the pytest process.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import cast

from entrypoints.run import supervise
from vibesys.api import RunResult, RunSession, RunStatus, StopMode
from vibesys.api.testing import FakeRunHandle
from vs_sim.api import LoopSignalSource
from vs_sim.api.testing import run_in_child


def _replacement_handler(signum: int, frame: object) -> None:
    """Stand-in for an import-time SIGINT handler that raises directly."""
    del signum, frame
    raise KeyboardInterrupt


class _SelfInterruptingSession:
    """A session whose running run is sent a real SIGINT."""

    def __init__(self) -> None:
        self.stop_requested = asyncio.Event()
        self.stops: list[str] = []

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def await_result(self) -> RunResult:
        os.kill(os.getpid(), signal.SIGINT)
        await self.stop_requested.wait()
        return RunResult(run_id="signalled", loop="test", succeeded=False, status=RunStatus.STOPPED)

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        self.stops.append(mode.value)
        self.stop_requested.set()


def _drain_after_real_sigint() -> tuple[RunStatus, list[str], bool]:
    signal.signal(signal.SIGINT, _replacement_handler)
    session = _SelfInterruptingSession()

    async def execute() -> RunStatus:
        handle = FakeRunHandle("signalled")
        handle.bind(cast("RunSession", session))
        handle.start()
        return (await supervise(handle, signals=LoopSignalSource())).status

    status = asyncio.run(execute())
    restored = signal.getsignal(signal.SIGINT) is _replacement_handler
    return status, session.stops, restored


def test_ctrl_c_drains_the_run_even_when_a_dependency_replaced_the_sigint_handler() -> None:
    """Ctrl-C reaches the run as one stop that cancels its work in flight (``StopMode.CANCEL``)."""
    status, stops, handler_restored = run_in_child(_drain_after_real_sigint)

    assert status is RunStatus.STOPPED
    assert stops == [StopMode.CANCEL.value]
    assert handler_restored
