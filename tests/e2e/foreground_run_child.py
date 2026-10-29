"""A foreground server run on a fake runtime, for signal tests.

Two ways to drive it, both through the public ``entrypoints.server.main``:

* ``python -m tests.e2e.foreground_run_child TRANSPORTS`` is the real entrypoint process: it
  announces the kernel ids of the threads it owns and a parent aims SIGTERM at one of them.
* :func:`interrupted_run` runs in a forked child of the test (``run_in_child``) and signals
  itself, so many thread interleavings cost no interpreter start-up.
"""

from __future__ import annotations

import asyncio
import faulthandler
import gc
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar

import server.runtime as runtime_module
from entrypoints import cli
from entrypoints.server import main
from vs_project.api import user_runtime_root
from vs_sim.api.testing import send_to_thread, wait_or_fail

if TYPE_CHECKING:
    from collections.abc import Callable


class BlockedRuntime:
    """A runtime with idle transport threads whose run never ends on its own."""

    transports = 1
    self_target: ClassVar[int | None] = None
    """Index of the owned thread this process signals itself, or ``None`` to await a parent."""
    discard_wake_in_gc: ClassVar[bool] = False
    """Deliver the first wake inside a GC callback that discards its exception."""
    cleanups: ClassVar[list[bool]] = []
    """For each finished run, whether shutdown had been requested by the time it cleaned up."""

    def __init__(self, **_options: object) -> None:
        self.stopping = threading.Event()

    def run(self, callback: Callable[[], object]) -> object:
        idle = threading.Event()
        for _ in range(self.transports):
            threading.Thread(target=idle.wait, daemon=True).start()
        try:
            return callback()
        finally:
            type(self).cleanups.append(self.stopping.is_set())
            print(f"cleanup shutdown={self.stopping.is_set()}", flush=True)  # noqa: T201  # lint-waiver: LW-155501 [T201]; the parent test reads the child's observations from stdout

    def drive(self, _request: object) -> None:
        async def serve_forever() -> None:
            # The threads the run owns (this one, the transports, the signal relay), by
            # kernel id: SIGTERM is aimed at these and at no library thread.
            owned = [
                thread.native_id for thread in threading.enumerate() if thread.native_id is not None
            ]
            print("driving", *owned, flush=True)  # noqa: T201  # lint-waiver: LW-155506 [T201]; the parent test reads the child's observations from stdout
            if self.discard_wake_in_gc:
                _signal_during_gc()
            elif self.self_target is not None:
                send_to_thread(os.getpid(), owned[self.self_target % len(owned)], signal.SIGTERM)
            await asyncio.sleep(0)
            await asyncio.Event().wait()

        asyncio.run(serve_forever())

    def interrupt(self) -> bool:
        """No run control is installed: a Ctrl-C ends the server as SIGTERM does."""
        return False

    def shutdown(self, number: signal.Signals | None = None) -> None:
        del number
        self.stopping.set()


def _signal_during_gc() -> None:
    """Keep the first wake inside a GC callback, where Python discards its exception."""
    entered = False

    def callback(phase: str, _info: dict[str, int]) -> None:
        nonlocal entered
        if phase != "start" or entered:
            return
        entered = True
        send_to_thread(os.getpid(), threading.get_native_id(), signal.SIGTERM)
        # Only the wake's KeyboardInterrupt releases this wait. The collector discards it.
        wait_or_fail(threading.Event(), "the first wake inside a GC callback")

    gc.callbacks.append(callback)
    try:
        gc.collect()
    finally:
        gc.callbacks.remove(callback)


def _serve(transports: int) -> None:
    """Swap the runtime and request building for a blocked fake, then run ``main``.

    Only ever called in a process that exists to run this one scenario.
    """
    BlockedRuntime.transports = transports
    # test-isolation: the child swaps the runtime and request parsing for a blocked fake
    runtime_module.ServerRuntime = BlockedRuntime  # ty: ignore[invalid-assignment]  # LW-155503 [invalid-assignment]; the child swaps the dynamically imported runtime class for its fake
    cli.parse_cli_invocation = lambda _argv: object()  # ty: ignore[invalid-assignment]  # LW-155504 [invalid-assignment]; the child replaces CLI parsing with a fixed fake
    cli.build_run_request = lambda _invocation: SimpleNamespace(  # ty: ignore[invalid-assignment]  # LW-155505 [invalid-assignment]; the child replaces request building with a fixed fake
        resolved_run_id="run-1",
        resolved_project_root=Path("/unused/project"),
    )
    # Inside the runtime root: the server refuses a control socket anywhere else.
    socket = user_runtime_root(os.environ, os.getuid()) / "unused" / "control.sock"
    main(["--control-socket", str(socket), "--local"])


def interrupted_run(
    transports: int, target: int, *, discard_wake_in_gc: bool = False
) -> tuple[list[bool], str]:
    """Serve a foreground run that signals itself; return its cleanups and how it ended.

    Call through ``run_in_child``: it installs signal handlers and swaps module attributes.
    """
    BlockedRuntime.self_target = target
    BlockedRuntime.discard_wake_in_gc = discard_wake_in_gc
    try:
        _serve(transports)
    except KeyboardInterrupt:
        ended = "interrupted"
    else:
        ended = "returned"
    return BlockedRuntime.cleanups, ended


if __name__ == "__main__":
    # SIGABRT from the parent dumps all thread stacks to stderr when the run fails to stop.
    faulthandler.enable()
    _serve(int(sys.argv[1]))
