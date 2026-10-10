"""A child process that serves a foreground run on a fake runtime, for signal tests."""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import TYPE_CHECKING

import server.runtime as runtime_module
from entrypoints import cli
from entrypoints.server import main

if TYPE_CHECKING:
    from collections.abc import Callable


class BlockedRuntime:
    """A runtime with idle transport threads whose run never ends on its own."""

    transports = 1

    def __init__(self, **_options: object) -> None:
        self.stopping = threading.Event()

    def run(self, callback: Callable[[], object]) -> object:
        idle = threading.Event()
        for _ in range(self.transports):
            threading.Thread(target=idle.wait, daemon=True).start()
        try:
            return callback()
        finally:
            print(f"cleanup shutdown={self.stopping.is_set()}", flush=True)  # noqa: T201  # lint-waiver: LW-155501 [T201]; the parent test reads the child's observations from stdout

    def drive(self, _request: object) -> None:
        async def serve_forever() -> None:
            # The threads the run owns (this one, the transports, the signal relay), by
            # kernel id: the parent aims SIGTERM at these and at no library thread.
            owned = " ".join(str(thread.native_id) for thread in threading.enumerate())
            print(f"driving {owned}", flush=True)  # noqa: T201  # lint-waiver: LW-155506 [T201]; the parent test reads the child's observations from stdout
            await asyncio.sleep(0)
            await asyncio.Event().wait()

        asyncio.run(serve_forever())

    def shutdown(self) -> None:
        self.stopping.set()


if __name__ == "__main__":
    # test-isolation: the child swaps the runtime and request parsing for a blocked fake
    BlockedRuntime.transports = int(sys.argv[1])
    runtime_module.ServerRuntime = BlockedRuntime  # ty: ignore[invalid-assignment]  # LW-155503 [invalid-assignment]; the child swaps the dynamically imported runtime class for its fake
    cli.parse_cli_invocation = lambda _argv: object()  # ty: ignore[invalid-assignment]  # LW-155504 [invalid-assignment]; the child replaces CLI parsing with a fixed fake
    cli.build_run_request = lambda _invocation: object()  # ty: ignore[invalid-assignment]  # LW-155505 [invalid-assignment]; the child replaces request building with a fixed fake
    main(["--control-socket", "/unused/control.sock", "--local"])
