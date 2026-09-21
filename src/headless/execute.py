"""Execute a `RunRequest` and render it to the terminal.

Owns "execute + render" for headless invocations: builds a session with the
`HeadlessRenderer` sink and awaits its result. Argument parsing and request
building stay in `entrypoints.cli`.
"""

from __future__ import annotations

import asyncio

from headless.render import HeadlessRenderer
from vibesys.api import RunRequest, RunResult, create_session


def run(request: RunRequest) -> RunResult:
    """Run *request* to completion through `vibesys.api.create_session`.

    `HeadlessRenderer().handle` is the sink. `create_session` emits this
    run's `RUN_STARTED`/`RUN_FINISHED`/`RUN_FAILED` lifecycle events onto it.
    """
    session = create_session(request, sink=HeadlessRenderer().handle)
    session.start()
    return asyncio.run(session.await_result())
