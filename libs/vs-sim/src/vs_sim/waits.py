"""Waits on a peer the test does not control, bounded so a stuck peer fails with a message.

``HANG_GUARD_S`` is far above any passing run, so reaching it means the peer is stuck.
It is a hang guard, not a synchronization tool: no test may rely on it to pass, and no
test asserts how long a wait took.
"""

from __future__ import annotations

import queue
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import multiprocessing
    import socket
    import threading

    from vs_sim.concurrency import Event

HANG_GUARD_S = 60.0


def join_or_fail(worker: threading.Thread | multiprocessing.process.BaseProcess) -> None:
    """Join *worker*, failing the test if it is still running after the guard bound."""
    worker.join(HANG_GUARD_S)
    if worker.is_alive():
        message = f"{worker!r} was still running after {HANG_GUARD_S:g} s"
        raise AssertionError(message)


def wait_or_fail(event: Event, what: str = "the event") -> None:
    """Wait for *event*, failing the test if *what* is still not set after the guard bound."""
    if not event.wait(HANG_GUARD_S):
        message = f"{what} was not set after {HANG_GUARD_S:g} s"
        raise AssertionError(message)


def get_or_fail[T](source: queue.Queue[T], what: str = "the queue") -> T:
    """Take the next item of *source*, failing the test if *what* stays empty past the guard bound."""
    try:
        return source.get(timeout=HANG_GUARD_S)
    except queue.Empty:
        message = f"{what} stayed empty for {HANG_GUARD_S:g} s"
        raise AssertionError(message) from None


def accept_or_fail(listener: socket.socket) -> tuple[socket.socket, Any]:
    """Accept one connection on *listener*, failing the test if none arrives within the guard bound."""
    listener.settimeout(HANG_GUARD_S)
    try:
        return listener.accept()
    except TimeoutError:
        message = f"no connection reached {listener.getsockname()} in {HANG_GUARD_S:g} s"
        raise AssertionError(message) from None


def stop_process(process: subprocess.Popen[Any], grace_s: float = HANG_GUARD_S) -> None:
    """Terminate *process* if it runs; kill it if it is still running *grace_s* later.

    The default grace is the hang guard, so a process that ignores SIGTERM is killed only
    once it has proven stuck; ``0`` kills one that does not stop at once.
    """
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=HANG_GUARD_S)
