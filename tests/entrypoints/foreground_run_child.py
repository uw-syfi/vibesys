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
            print("driving", flush=True)  # noqa: T201  # lint-waiver: LW-155501 [T201]; the parent test reads the child's observations from stdout
            await asyncio.sleep(0)
            await asyncio.Event().wait()

        asyncio.run(serve_forever())

    def shutdown(self) -> None:
        self.stopping.set()


if __name__ == "__main__":
    # test-isolation: the child swaps the runtime and request parsing for a blocked fake
    BlockedRuntime.transports = int(sys.argv[1])
    runtime_module.ServerRuntime = BlockedRuntime  # type: ignore[misc]
    cli.parse_cli_invocation = lambda _argv: object()  # type: ignore[assignment]
    cli.build_run_request = lambda _invocation: object()  # type: ignore[assignment]
    main(["--control-socket", "/unused/control.sock", "--local"])
