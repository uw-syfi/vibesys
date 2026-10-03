"""Execute a `RunRequest` and render it to the terminal.

Owns "execute + render" for headless invocations: builds a session with the
`HeadlessRenderer` sink and awaits its result. Argument parsing and request
building stay in `entrypoints.cli`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading

from headless.render import HeadlessRenderer
from vibesys.api import RunRequest, RunResult, RunSession, RunStopped, create_session


async def _await_interruptibly(session: RunSession) -> RunResult:
    """Turn task cancellation into a cooperative stop before shutdown."""
    execution = asyncio.create_task(session.await_result())
    try:
        return await asyncio.shield(execution)
    except asyncio.CancelledError:
        session.stop()
        with contextlib.suppress(RunStopped):
            await asyncio.shield(execution)
        raise


def _run_interruptibly(session: RunSession) -> RunResult:
    """Run the session so that the first Ctrl-C requests a cooperative stop.

    `asyncio.run` turns SIGINT into cancellation of the main task (which
    `_await_interruptibly` converts into a drain) only while the default
    SIGINT handler is installed. A dependency may replace it at import time
    (the Docker sandbox backend does, to unwind before its atexit cleanup);
    then Ctrl-C raises `KeyboardInterrupt` inside the event loop and the run
    ends at once without stopping, abandoning in-flight work. The default
    handler raises the same `KeyboardInterrupt` that replacement relies on,
    so it is restored for the run and the prior handler reinstated after.
    """
    if threading.current_thread() is not threading.main_thread():
        return asyncio.run(_await_interruptibly(session))
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        return asyncio.run(_await_interruptibly(session))
    finally:
        signal.signal(signal.SIGINT, previous)


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
