"""Bounded joins for tests that wait on a thread or process they do not control.

A bare ``thread.join()`` parks the test forever when the thread never ends, and
the failure names nothing. ``HANG_GUARD_S`` is far above any passing run, so
reaching it means the peer is stuck; it is a hang guard, not a synchronization
tool, and no test may rely on it to pass.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import multiprocessing
    import threading

HANG_GUARD_S = 60.0


def join_or_fail(worker: threading.Thread | multiprocessing.process.BaseProcess) -> None:
    """Join *worker*, failing the test if it is still running after the guard bound."""
    worker.join(HANG_GUARD_S)
    assert not worker.is_alive(), f"{worker!r} was still running after {HANG_GUARD_S:g} s"


def stop_process(process: subprocess.Popen[Any]) -> None:
    """Terminate *process* if it runs; escalate to kill if it ignores SIGTERM."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=HANG_GUARD_S)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=HANG_GUARD_S)
