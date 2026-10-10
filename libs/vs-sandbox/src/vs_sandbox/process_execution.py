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
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from vs_sim.api import OsThreads, Threads

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


#: The signals a stop sends, named here so only this adapter module refers to ``signal``.
type StopSignal = signal.Signals
TERMINATE: StopSignal = signal.SIGTERM
KILL: StopSignal = signal.SIGKILL
#: Delivers a signal to the processes of a command that live outside the launched process group.
type SignalSender = Callable[[StopSignal], None]

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
    """Captured streams and exit status of one finished or stopped command.

    ``returncode`` is the shell-convention status: ``128 + N`` for a process
    killed by signal ``N``, never a negative number. The streams hold
    everything the command wrote before it exited or was stopped.
    """

    stdout: str
    stderr: str
    returncode: int
    stopped: ProcessStop | None = None


def shell_exit_status(returncode: int) -> int:
    """Return the shell-convention status of a ``subprocess`` return code.

    ``subprocess`` reports a signalled child as ``-N``; a shell (and ``docker
    exec``) reports ``128 + N``. One convention keeps ``kill -9`` the same
    status (137) in every sandbox kind.
    """
    return 128 - returncode if returncode < 0 else returncode


class CancelSignal(Protocol):
    """What a stoppable wait reads from its caller's cancel event (``vs_sim.api.Event`` has it)."""

    def is_set(self) -> bool:
        """Whether the caller asked to stop."""
        ...


class StoppableProcess(Protocol):
    """A started process group leader that :func:`wait_stoppable` can wait for and stop.

    :class:`PopenProcess` is the real one; ``FakeStoppableProcess`` in
    ``vs_sandbox.api.testing`` runs on a simulated clock. Both pass the cases in
    :class:`~vs_sandbox.process_contracts.StoppableProcessContract`.
    """

    @property
    def returncode(self) -> int:
        """The ``subprocess`` return code (``-N`` for signal ``N``) once the wait has returned output."""
        ...

    def wait(self, timeout: float | None) -> tuple[str, str] | None:
        """Wait up to *timeout* seconds; ``(stdout, stderr)`` once it exited, else ``None``.

        Output accumulates across calls, so a later call returns everything the
        process wrote.
        """
        ...

    def signal_group(self, signal_number: StopSignal) -> None:
        """Send *signal_number* to the process group; a no-op once nothing is left in it."""
        ...


class PopenProcess:
    """A ``subprocess.Popen`` started with :func:`start_process_group`."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        """Wrap *process*, the leader of its own process group."""
        self._process = process

    @property
    def returncode(self) -> int:
        """The return code of the exited leader."""
        code = self._process.returncode
        if code is None:
            message = "the process has not exited"
            raise RuntimeError(message)
        return code

    def wait(self, timeout: float | None) -> tuple[str, str] | None:
        """Wait with ``communicate``, which keeps what was read when the wait times out."""
        try:
            return self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def signal_group(self, signal_number: StopSignal) -> None:
        """Signal the group; it outlives its leader while any descendant remains in it."""
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, signal_number)


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
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        process_group=0,
    )


def wait_stoppable(  # noqa: PLR0913  # lint-waiver: LW-731015 [PLR0913]; the stop policy is five independent keywords (timeout, cancel, grace, remote signal, clock) and bundling them would only rename them.
    process: StoppableProcess,
    *,
    timeout: float | None,
    cancel: CancelSignal | None,
    grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    signal_remote: SignalSender | None = None,
    threads: Threads | None = None,
) -> ProcessOutcome:
    """Wait for a process group leader until it exits, times out, or is cancelled.

    On a stop, *signal_remote* receives ``SIGTERM`` first (a backend whose
    command lives outside this process group, such as a container exec,
    signals it there), then the group receives ``SIGTERM`` and, after
    *grace_seconds*, *signal_remote* and the group receive ``SIGKILL``. The
    outcome keeps the output written so far and names the stop reason. If the
    waiting thread is interrupted (``KeyboardInterrupt``), the command is
    killed before the interruption propagates, so nothing outlives the call.
    *threads* supplies the clock the timeout is measured on.
    """
    clock = (threads or OsThreads()).now
    deadline = None if timeout is None else clock() + timeout
    try:
        while True:
            remaining = None if deadline is None else max(0.0, deadline - clock())
            wait = remaining if cancel is None else _min_wait(remaining)
            finished = process.wait(wait)
            if finished is not None:
                return ProcessOutcome(*finished, shell_exit_status(process.returncode))
            if cancel is not None and cancel.is_set():
                stop = ProcessStop.CANCELLED
            elif deadline is not None and clock() >= deadline:
                stop = ProcessStop.TIMEOUT
            else:
                continue
            return _stop(process, stop, grace_seconds, signal_remote)
    except BaseException:
        _kill(process, signal_remote)
        raise


def _stop(
    process: StoppableProcess,
    stop: ProcessStop,
    grace_seconds: float,
    signal_remote: SignalSender | None,
) -> ProcessOutcome:
    stop_signal = TERMINATE
    if signal_remote is not None:
        signal_remote(stop_signal)
    process.signal_group(stop_signal)
    finished = process.wait(grace_seconds)
    if finished is None:
        _kill(process, signal_remote)
        finished = process.wait(None)
    elif signal_remote is not None:
        # The local client is gone. A command whose remote process started after the
        # first request found nothing to signal (its client was killed before it
        # existed there) is reachable only now, and nothing else would stop it.
        signal_remote(stop_signal)
    if finished is None:
        message = "a killed process did not exit"
        raise RuntimeError(message)
    return ProcessOutcome(*finished, shell_exit_status(process.returncode), stop)


def _kill(process: StoppableProcess, signal_remote: SignalSender | None) -> None:
    if signal_remote is not None:
        signal_remote(KILL)
    process.signal_group(KILL)


def _min_wait(remaining: float | None) -> float:
    return _CANCEL_POLL_SECONDS if remaining is None else min(_CANCEL_POLL_SECONDS, remaining)
