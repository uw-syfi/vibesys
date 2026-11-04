"""Process supervision for a started run, owned by the process entrypoint."""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING

from headless import printable
from headless import run as render_run
from vibesys.api import RunStopped, StopMode, relaunch
from vs_sim.api import Signals

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterator

    from vibesys.api import RunHandle, RunRequest, RunResult, Runs
    from vs_sim.api import SignalSource

# Ctrl-C asks for a stop that cancels the work in flight (Slurm jobs) at once,
# bounded like every stop, even after a draining stop: the run's control is the
# one authority for its stop, and a stop there only escalates. The launcher
# forwards each Ctrl-C once (this process leads its own session, so the
# terminal's copy never reaches it) and sends a repeated one on as SIGTERM.
# SIGTERM and SIGHUP end the run now: its task is cancelled, so its teardown
# cancels external work before the process exits.
__all__ = ["failure_reason", "run_headless", "supervise"]

_TERMINATION_SIGNALS = (Signals.SIGTERM, Signals.SIGHUP)
_HANDLED_SIGNALS = (Signals.SIGINT, *_TERMINATION_SIGNALS)


class _Supervisor:
    """Turn stop requests and process signals into one run's stop or cancellation."""

    def __init__(self, session: RunHandle, execution: asyncio.Task[RunResult]) -> None:
        self._session = session
        self._execution = execution
        self.terminating = False
        self.signalled: Signals | None = None

    def stop(self, mode: StopMode = StopMode.DRAIN) -> None:
        """Request a cooperative stop in ``mode``; the run's control keeps the strongest.

        A repeated request changes nothing, and a cancel after a drain escalates it.
        """
        self._session.stop(mode)

    def terminate(self) -> None:
        """Cancel the run once; cancellation unwinds through its teardown."""
        if not self.terminating:
            self.terminating = True
            self._session.cancel()

    def on_signal(self, number: Signals) -> None:
        """Stop, cancelling work in flight, on Ctrl-C; cancel the run on any other signal."""
        self.signalled = self.signalled or number
        if number is Signals.SIGINT:
            if not self.terminating:
                self.stop(StopMode.CANCEL)
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
    signals: SignalSource | None,
) -> RunResult:
    """Await the run; a stop or signal unwinds it through its own teardown.

    Cancelling this coroutine requests a cooperative stop and drains the run
    before the cancellation propagates. With a *signals* source, SIGINT requests a
    stop in ``StopMode.CANCEL`` (the run cancels its evaluations and jobs at once,
    and the host bounds how long its agents' turns may take to end) and returns the
    typed stopped result. If cancelling this coroutine already asked for a draining
    stop, a SIGINT escalates it to ``StopMode.CANCEL`` and the cancellation still
    propagates once the run has ended. A repeated SIGINT changes nothing. SIGTERM or SIGHUP,
    first or after a SIGINT, cancel the run and end in ``SystemExit(128 + n)``,
    where n is the first signal received.
    The handlers run on the event loop, so no signal raises inside a teardown step.
    *signals* is where they arrive: the process's own (``LoopSignalSource``, built
    by the entrypoint), a Fake that a test delivers by hand, or None to handle none.
    """
    execution = asyncio.ensure_future(work if work is not None else session.result())
    supervisor = _Supervisor(session, execution)
    with _signal_scope(supervisor, signals):
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
def _signal_scope(supervisor: _Supervisor, signals: SignalSource | None) -> Iterator[None]:
    """Route the handled signals to *supervisor* for the scope, then hand them back."""
    if signals is None:
        yield
        return
    installed: list[Signals] = []
    try:
        for number in _HANDLED_SIGNALS:
            signals.add_handler(number, partial(supervisor.on_signal, number))
            installed.append(number)
        yield
    finally:
        for number in installed:
            signals.remove_handler(number)


def _signal_exit(number: Signals) -> BaseException:
    return SystemExit(128 + number)


def run_headless(request: RunRequest, runs: Runs, signals: SignalSource) -> RunResult:
    """Start one run and supervise terminal rendering through completion.

    *signals* is where the process's stop and termination signals arrive.
    """

    async def execute() -> RunResult:
        handle = relaunch(request, runs) if request.resume is not None else runs.start(request)
        return await supervise(handle, render_run(handle), signals=signals)

    try:
        return asyncio.run(execute())
    except RunStopped:
        raise
    except Exception as error:
        # The traceback still follows; this is the one line a reader of the output needs.
        sys.stderr.write(f"run failed: {printable(failure_reason(error))}\n")
        raise


def failure_reason(error: BaseException) -> str:
    """The typed reason a run failed: the error's type, then its message on one line."""
    message = " ".join(str(error).split())
    return f"{type(error).__name__}: {message}" if message else type(error).__name__
