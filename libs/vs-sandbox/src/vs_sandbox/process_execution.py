"""Run one sandbox command process that its caller can stop.

The process starts in its own process group so stopping it reaches every
descendant (a shell's children included), not only the direct child. A stop,
whether from the caller's cancel event or from the timeout, sends ``SIGTERM``
to the group first so a descendant can release what it holds (for example a
Slurm wrapper cancelling its submitted job), then ``SIGKILL`` after a grace
period.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Mapping, Sequence

#: Default time a stopped command gets between ``SIGTERM`` and ``SIGKILL``.
DEFAULT_TERMINATION_GRACE_SECONDS = 30.0
# How often a cancellable wait checks the caller's event. The event has no
# wait-any primitive with process exit, so the wait polls; this bounds the
# latency of a cancellation, not of a normal exit.
_CANCEL_POLL_SECONDS = 0.05


class ProcessStop(StrEnum):
    """Why a command was stopped before it exited on its own."""

    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Captured streams and exit status of one finished or stopped command."""

    stdout: str
    stderr: str
    returncode: int
    stopped: ProcessStop | None = None


def start_process_group(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    cwd: str | None,
) -> subprocess.Popen[str]:
    """Start *argv* as the leader of a new process group with piped output.

    Launch failures raise the ``subprocess`` error unchanged.
    """
    return subprocess.Popen(  # noqa: S603  # lint-waiver: LW-731001 [S603]; the sandbox boundary intentionally executes its caller's command.
        # > Each sandbox assembles its own argv (a shell command is ``sh -c``);
        # > validating argv here would duplicate each sandbox's command contract.
        argv,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        process_group=0,
    )


def wait_stoppable(
    process: subprocess.Popen[str],
    *,
    timeout: float | None,
    cancel: threading.Event | None,
    grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    before_stop: Callable[[], None] | None = None,
) -> ProcessOutcome:
    """Wait for a process group leader until it exits, times out, or is cancelled.

    On a stop, *before_stop* runs first (a backend whose command lives
    outside this process group, such as a container exec, stops it there),
    then the group receives ``SIGTERM`` and, after *grace_seconds*,
    ``SIGKILL``. The outcome names the stop reason.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        wait = remaining if cancel is None else _min_wait(remaining)
        try:
            stdout, stderr = process.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                stop = ProcessStop.CANCELLED
            elif deadline is not None and time.monotonic() >= deadline:
                stop = ProcessStop.TIMEOUT
            else:
                continue
            break
        return ProcessOutcome(stdout, stderr, process.returncode)
    if before_stop is not None:
        before_stop()
    _signal_group(process, signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        stdout, stderr = process.communicate()
    return ProcessOutcome(stdout, stderr, process.returncode, stop)


def _min_wait(remaining: float | None) -> float:
    return _CANCEL_POLL_SECONDS if remaining is None else min(_CANCEL_POLL_SECONDS, remaining)


def _signal_group(process: subprocess.Popen[str], signal_number: signal.Signals) -> None:
    # The group outlives its leader while any descendant remains in it.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal_number)
