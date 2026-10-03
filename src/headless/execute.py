"""Execute a `RunRequest` and render it to the terminal.

Owns "execute + render" for headless invocations: builds a session with the
`HeadlessRenderer` sink and awaits its result. Argument parsing and request
building stay in `entrypoints.cli`.
"""

from __future__ import annotations

import asyncio
import signal
import threading

from headless.render import HeadlessRenderer
from vibesys.api import RunRequest, RunResult, RunSession, RunStopped, create_session

# Ctrl-C asks for a cooperative stop. These, and any repeated signal, end the
# run now: its task is cancelled, so its teardown cancels external work (Slurm
# jobs) before the process exits.
_TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
_HANDLED_SIGNALS = (signal.SIGINT, *_TERMINATION_SIGNALS)


class _Supervisor:
    """Turn stop requests and process signals into one run's stop or cancellation."""

    def __init__(self, session: RunSession, execution: asyncio.Task[RunResult]) -> None:
        self._session = session
        self._execution = execution
        self._stopping = False
        self._terminating = False
        self.signalled: signal.Signals | None = None

    def stop(self) -> None:
        """Request a cooperative stop once; the run drains in-flight work."""
        if not self._stopping:
            self._stopping = True
            self._session.stop()

    def terminate(self) -> None:
        """Cancel the run once; cancellation unwinds through its teardown."""
        if not self._terminating:
            self._terminating = True
            self._execution.cancel()

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


async def _await_interruptibly(session: RunSession, *, handle_signals: bool = False) -> RunResult:
    """Await the run; a stop or signal unwinds it through its own teardown.

    Cancelling this coroutine requests a cooperative stop and drains the run
    before the cancellation propagates. With *handle_signals*, the first SIGINT
    requests that stop and ends in ``KeyboardInterrupt``; SIGTERM, SIGHUP, or a
    repeated signal cancel the run instead and end in ``SystemExit(128 + n)``
    (``KeyboardInterrupt`` for SIGINT). The handlers run on the event loop, so
    no signal ever raises inside a teardown step.
    """
    execution = asyncio.create_task(session.await_result())
    supervisor = _Supervisor(session, execution)
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    if handle_signals:
        for number in _HANDLED_SIGNALS:
            loop.add_signal_handler(number, supervisor.on_signal, number)
            installed.append(number)
    try:
        try:
            return await asyncio.shield(execution)
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
    finally:
        for number in installed:
            loop.remove_signal_handler(number)


def _signal_exit(number: signal.Signals) -> BaseException:
    if number is signal.SIGINT:
        return KeyboardInterrupt()
    return SystemExit(128 + number)


def _run_interruptibly(session: RunSession) -> RunResult:
    """Run the session so that process signals stop it through its teardown.

    `_await_interruptibly` handles SIGINT, SIGTERM, and SIGHUP on the event
    loop for the run's duration. A dependency may replace the SIGINT handler at
    import time (the Docker sandbox backend does, to unwind before its atexit
    cleanup); every prior handler is reinstated after the run.
    """
    if threading.current_thread() is not threading.main_thread():
        return asyncio.run(_await_interruptibly(session))
    previous = {number: signal.getsignal(number) for number in _HANDLED_SIGNALS}
    try:
        return asyncio.run(_await_interruptibly(session, handle_signals=True))
    finally:
        for number, handler in previous.items():
            if handler is not None:
                signal.signal(number, handler)


def run(request: RunRequest) -> RunResult:
    """Run *request* to completion through `vibesys.api.create_session`.

    `HeadlessRenderer().handle` is the sink. `create_session` emits this
    run's `RUN_STARTED`/`RUN_FINISHED`/`RUN_FAILED` lifecycle events onto it.
    """
    session = create_session(request, sink=HeadlessRenderer().handle)
    session.start()
    try:
        return _run_interruptibly(session)
    finally:
        session.close()
