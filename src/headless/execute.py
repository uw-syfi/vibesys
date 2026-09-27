"""Execute a `RunRequest` and render it to the terminal.

Owns "execute + render" for headless invocations: builds a session with the
`HeadlessRenderer` sink and awaits its result. Argument parsing and request
building stay in `entrypoints.cli`.
"""

from __future__ import annotations

import asyncio
import contextlib

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


def run(request: RunRequest) -> RunResult:
    """Run *request* to completion through `vibesys.api.create_session`.

    `HeadlessRenderer().handle` is the sink. `create_session` emits this
    run's `RUN_STARTED`/`RUN_FINISHED`/`RUN_FAILED` lifecycle events onto it.
    """
    session = create_session(request, sink=HeadlessRenderer().handle)
    session.start()
    try:
        return asyncio.run(_await_interruptibly(session))
    finally:
        session.close()
