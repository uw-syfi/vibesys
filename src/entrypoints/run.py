"""Process supervision for a started run, owned by the process entrypoint."""

from __future__ import annotations

import asyncio
import signal
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

from headless import run as render_run
from vibesys.api import RunStopped

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterator

    from vibesys.api import RunHandle, RunRequest, RunResult, Runs

# Ctrl-C asks for a cooperative stop. These, and any repeated signal, end the
# run now: its task is cancelled, so its teardown cancels external work (Slurm
# jobs) before the process exits.
__all__ = ["run_headless", "supervise"]

_TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
_HANDLED_SIGNALS = (signal.SIGINT, *_TERMINATION_SIGNALS)


class _Supervisor:
    """Turn stop requests and process signals into one run's stop or cancellation."""

    def __init__(self, session: RunHandle, execution: asyncio.Task[RunResult]) -> None:
        self._session = session
        self._execution = execution
        self._stopping = False
        self.terminating = False
        self.signalled: signal.Signals | None = None

    def stop(self) -> None:
        """Request a cooperative stop once; the run drains in-flight work."""
        if not self._stopping:
            self._stopping = True
            self._session.stop()

    def terminate(self) -> None:
        """Cancel the run once; cancellation unwinds through its teardown."""
        if not self.terminating:
            self.terminating = True
            self._session.cancel()

    def on_signal(self, number: signal.Signals) -> None:
        """Stop on the first Ctrl-C; cancel on any other or repeated signal."""
        first = self.signalled is None
        self.signalled = self.signalled or number
        if first and number is signal.SIGINT:
            self.stop()
        else:
            self.terminate()

    async def drain(self) -> None:
        """Wait for the run to finish; a cancellation of this wait escalates."""
        while not self._execution.done():
            try:
                await asyncio.wait({self._execution})
            except asyncio.CancelledError:
                self.terminate()


async def supervise(
    session: RunHandle,
    work: Awaitable[RunResult] | None = None,
    *,
    handle_signals: bool = True,
) -> RunResult:
    """Await the run; a stop or signal unwinds it through its own teardown.

    Cancelling this coroutine requests a cooperative stop and drains the run
    before the cancellation propagates. With *handle_signals*, the first SIGINT
    requests that stop and returns the typed stopped result; SIGTERM, SIGHUP,
    or a repeated signal cancel the run and end in ``SystemExit(128 + n)``.
    The handlers run on the event loop, so no signal raises inside a teardown step.
    """
    execution = asyncio.ensure_future(work if work is not None else session.result())
    supervisor = _Supervisor(session, execution)
    with _signal_scope(supervisor, enabled=handle_signals):
        completed = False
        try:
            result = await _await_completion(supervisor, execution)
            completed = True
            return result
        finally:
            # The renderer can fail while execution is still active. Drain the
            # run here so event-loop shutdown cannot interrupt its cleanup.
            if not completed and supervisor.signalled is None:
                supervisor.stop()
            session.cancel()
            await asyncio.gather(session.result(), return_exceptions=True)


async def _await_completion(
    supervisor: _Supervisor, execution: asyncio.Future[RunResult]
) -> RunResult:
    try:
        result = await asyncio.shield(execution)
        if supervisor.signalled is None or not supervisor.terminating:
            return result
    except asyncio.CancelledError:
        if execution.done() and not execution.cancelled():
            raise
        if not execution.done():
            supervisor.stop()
        await supervisor.drain()
        current = asyncio.current_task()
        if supervisor.signalled is None or (current is not None and current.cancelling()):
            raise
    except RunStopped:
        if supervisor.signalled is None:
            raise
    await supervisor.drain()
    raise _signal_exit(supervisor.signalled)


@contextmanager
def _signal_scope(supervisor: _Supervisor, *, enabled: bool) -> Iterator[None]:
    """Temporarily install event-loop callbacks and restore process signals."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    previous = {}
    try:
        if enabled and threading.current_thread() is threading.main_thread():
            for number in _HANDLED_SIGNALS:
                previous[number] = signal.getsignal(number)
                loop.add_signal_handler(number, supervisor.on_signal, number)
                installed.append(number)
        yield
    finally:
        for number in installed:
            loop.remove_signal_handler(number)
            if previous[number] is not None:
                signal.signal(number, previous[number])


def _signal_exit(number: signal.Signals) -> BaseException:
    return SystemExit(128 + number)


def run_headless(request: RunRequest, runs: Runs) -> RunResult:
    """Start one run and supervise terminal rendering through completion."""

    async def execute() -> RunResult:
        handle = runs.resume(request) if request.resume is not None else runs.start(request)
        return await supervise(handle, render_run(handle))

    return asyncio.run(execute())
