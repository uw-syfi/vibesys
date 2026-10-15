"""Run one sandbox command process that its caller can stop.

The process starts in its own process group so stopping it reaches every
descendant (a shell's children included), not only the direct child. A stop,
whether from the caller's cancel event or from the timeout, sends ``SIGTERM``
to the group first so a descendant can release what it holds (for example a
Slurm wrapper cancelling its submitted job), then ``SIGKILL`` after a grace
period.
"""

from __future__ import annotations

import signal
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Protocol

from vs_sim.api import ProcessTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import TextIOBase

    from vs_sim.api import PipedProcess, Threads

#: The signals a stop sends, named here so only this adapter module refers to ``signal``.
type StopSignal = signal.Signals
TERMINATE: StopSignal = signal.SIGTERM
KILL: StopSignal = signal.SIGKILL


@dataclass(frozen=True, slots=True)
class RemoteProcesses:
    """What signalling a command's remote processes found: how many remain, if known.

    ``survivors`` is ``None`` when the signal could not be delivered or its answer
    could not be read, so nothing is known about the remote side.
    """

    survivors: int | None

    @property
    def gone(self) -> bool:
        """Whether the remote side was shown to hold none of the command's processes."""
        return self.survivors == 0


#: Signals the processes of a command that live outside the launched process group (a
#: container exec's) and says how many of them remain.
type SignalSender = Callable[[StopSignal], RemoteProcesses]

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
    CLIENT_LOST = "client_lost"
    """The launched process was the client of a remote command, and a signal this
    module did not send ended it. The client's death says nothing about the
    command, which may still be running where it lives, so it is stopped there."""


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Captured streams and exit status of one finished or stopped command.

    ``returncode`` is the shell-convention status: ``128 + N`` for a process
    killed by signal ``N``, never a negative number. It is ``None`` when the
    command's own status is unknown (:attr:`ProcessStop.CLIENT_LOST`). The
    streams hold everything the command wrote before it exited or was stopped.
    """

    stdout: str
    stderr: str
    returncode: int | None
    stopped: ProcessStop | None = None
    unconfirmed: RemoteProcesses | None = None
    """Set when a stop could not show that the command's remote processes ended."""


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

    :class:`PipedStoppableProcess` is the real one; ``FakeStoppableProcess`` in
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


class PipedStoppableProcess:
    """A :class:`StoppableProcess` over a started ``vs_sim`` :class:`~vs_sim.api.PipedProcess`.

    Two drain workers read stdout and stderr as the command writes them, so
    neither pipe fills and blocks the command; a wait returns the output once
    the leader exited and both pipes reached EOF. Like ``subprocess`` text
    mode, CRLF and a lone CR become LF.
    """

    def __init__(self, process: PipedProcess, *, threads: Threads) -> None:
        """Take ownership of *process* and start draining its pipes on *threads*."""
        self._process = process
        self._threads = threads
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._returncode: int | None = None
        self._drains = (
            threads.spawn(
                partial(_drain, process.stdout, self._stdout), name="vibesys-command-stdout"
            ),
            threads.spawn(
                partial(_drain, process.stderr, self._stderr), name="vibesys-command-stderr"
            ),
        )

    @property
    def returncode(self) -> int:
        """The return code of the exited leader."""
        if self._returncode is None:
            message = "the process has not exited"
            raise RuntimeError(message)
        return self._returncode

    def wait(self, timeout: float | None) -> tuple[str, str] | None:
        """Wait for both pipes' EOF and then the leader, within *timeout* seconds in total.

        The pipes come first: reaping the leader ends the group's signals, so a
        descendant that still holds a pipe open after the leader exited stays
        reachable by a stop until the pipes close.
        """
        deadline = None if timeout is None else self._threads.now() + timeout

        def remaining() -> float | None:
            return None if deadline is None else max(0.0, deadline - self._threads.now())

        for drain in self._drains:
            drain.join(remaining())
            if drain.is_alive():
                return None
        if self._returncode is None:
            try:
                self._returncode = self._process.wait(remaining())
            except ProcessTimeoutError:
                return None
        return _universal_newlines(self._stdout), _universal_newlines(self._stderr)

    def signal_group(self, signal_number: StopSignal) -> None:
        """Send ``SIGTERM`` or ``SIGKILL`` to the group while its leader is unreaped."""
        if signal_number == TERMINATE:
            self._process.terminate_group()
        elif signal_number == KILL:
            self._process.kill_group()
        else:
            message = f"a stoppable process is stopped only with SIGTERM or SIGKILL, not {signal_number!r}"
            raise ValueError(message)


_DRAIN_CHARACTERS = 65_536


def _drain(stream: TextIOBase, sink: list[str]) -> None:
    while chunk := stream.read(_DRAIN_CHARACTERS):
        sink.append(chunk)


def _universal_newlines(chunks: list[str]) -> str:
    return "".join(chunks).replace("\r\n", "\n").replace("\r", "\n")


def wait_stoppable(  # noqa: PLR0913  # lint-waiver: LW-731015 [PLR0913]; the stop policy is five independent keywords (timeout, cancel, grace, remote signal, clock) and bundling them would only rename them.
    process: StoppableProcess,
    *,
    timeout: float | None,
    cancel: CancelSignal | None,
    grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    signal_remote: SignalSender | None = None,
    threads: Threads,
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
    clock = threads.now
    deadline = None if timeout is None else clock() + timeout
    try:
        while True:
            remaining = None if deadline is None else max(0.0, deadline - clock())
            wait = remaining if cancel is None else _min_wait(remaining)
            finished = process.wait(wait)
            if finished is not None:
                if signal_remote is not None and process.returncode < 0:
                    return _client_lost(finished, grace_seconds, signal_remote, threads)
                return ProcessOutcome(*finished, shell_exit_status(process.returncode))
            if cancel is not None and cancel.is_set():
                stop = ProcessStop.CANCELLED
            elif deadline is not None and clock() >= deadline:
                stop = ProcessStop.TIMEOUT
            else:
                continue
            return _stop(process, stop, grace_seconds, signal_remote, threads)
    except BaseException:
        _kill(process, signal_remote)
        raise


def _stop(
    process: StoppableProcess,
    stop: ProcessStop,
    grace_seconds: float,
    signal_remote: SignalSender | None,
    threads: Threads,
) -> ProcessOutcome:
    grace_end = threads.now() + grace_seconds
    if signal_remote is not None:
        signal_remote(TERMINATE)
    process.signal_group(TERMINATE)
    finished = process.wait(grace_seconds)
    killed = finished is None
    if killed:
        _kill(process, signal_remote)
        finished = process.wait(None)
    if finished is None:
        message = "a killed process did not exit"
        raise RuntimeError(message)
    unconfirmed = None
    if signal_remote is not None:
        remote = _end_remote(signal_remote, grace_end, threads, killed=killed)
        unconfirmed = None if remote.gone else remote
    return ProcessOutcome(*finished, shell_exit_status(process.returncode), stop, unconfirmed)


def _client_lost(
    finished: tuple[str, str],
    grace_seconds: float,
    signal_remote: SignalSender,
    threads: Threads,
) -> ProcessOutcome:
    """Stop a remote command whose client a signal ended, and report no status for it.

    A client relays its remote command's status as an ordinary exit code, so a
    client ended by a signal is the client's own death (an OOM kill on the host,
    an operator's ``kill``), not the command's. The command may still run, so it
    gets ``SIGTERM`` and, after the grace period, ``SIGKILL`` where it lives.
    """
    remote = _end_remote(signal_remote, threads.now() + grace_seconds, threads, killed=False)
    unconfirmed = None if remote.gone else remote
    return ProcessOutcome(*finished, None, ProcessStop.CLIENT_LOST, unconfirmed)


def _end_remote(
    signal_remote: SignalSender, grace_end: float, threads: Threads, *, killed: bool
) -> RemoteProcesses:
    """Stop what remains of the command where it runs, now that its local client is gone.

    Killing the local client does not stop the remote command, so the remote side is
    signalled again and checked. ``SIGTERM`` reaches a remote process that started
    after the first request (its client was killed before it existed there); whatever
    outlives the grace period gets ``SIGKILL``.
    """
    remote = signal_remote(KILL if killed else TERMINATE)
    if remote.gone or killed:
        return remote
    threads.sleep(max(0.0, grace_end - threads.now()))
    return signal_remote(KILL)


def _kill(process: StoppableProcess, signal_remote: SignalSender | None) -> None:
    if signal_remote is not None:
        signal_remote(KILL)
    process.signal_group(KILL)


def _min_wait(remaining: float | None) -> float:
    return _CANCEL_POLL_SECONDS if remaining is None else min(_CANCEL_POLL_SECONDS, remaining)
