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
from math import isfinite
from typing import TYPE_CHECKING, Protocol

from vs_sim.api import OsThreads, ProcessTimeoutError, Threads

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

type _TextProcess = subprocess.Popen[str]


#: The signals a stop sends, named here so only this adapter module refers to ``signal``.
type StopSignal = signal.Signals
TERMINATE: StopSignal = signal.SIGTERM
KILL: StopSignal = signal.SIGKILL
#: Delivers a signal to the processes of a command that live outside the launched process group.
type SignalSender = Callable[[StopSignal], None]

#: Default time a stopped command gets between ``SIGTERM`` and ``SIGKILL``.
DEFAULT_TERMINATION_GRACE_SECONDS = 30.0
#: Longest cleanup wait after a synchronous command timeout kills its owned group.
DEFAULT_PROCESS_SETTLE_SECONDS = 5.0
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


class CapturedProcess(StoppableProcess, Protocol):
    """A process whose captured streams can be released after its leader is reaped."""

    @property
    def timeout_output(self) -> tuple[bytes | None, bytes | None]:
        """Raw stdout and stderr captured when the first wait timed out."""
        ...

    def signal_group_if_unreaped(self, signal_number: StopSignal) -> bool:
        """Signal the group only while the leader's PID still reserves its PGID.

        Return whether the leader was still unreaped and the signal was
        attempted. Once the leader is reaped, its numeric process-group ID may
        belong to an unrelated group and must never be signalled.
        """
        ...

    def close(self) -> None:
        """Attempt every pipe close, grouping multiple failures in stream order."""
        ...


@dataclass(frozen=True, slots=True)
class CapturedProcessRequest:
    """One synchronous captured command and its unchanged subprocess inputs."""

    argv: Sequence[str]
    stdin: str | None
    timeout: float
    env: Mapping[str, str] | None = None
    cwd: str | None = None
    start_new_session: bool = False


@dataclass(frozen=True, slots=True)
class CapturedProcessResult:
    """Complete captured streams and raw subprocess status for one command."""

    returncode: int
    stdout: str
    stderr: str


class CapturedProcessLauncher(Protocol):
    """Start a captured command as the leader of a locally owned process group."""

    def start(self, request: CapturedProcessRequest) -> CapturedProcess:
        """Start *request*, raising its launch error unchanged."""
        ...


class CapturedProcessRunner(Protocol):
    """Run captured commands while owning their whole local process group."""

    def run(self, request: CapturedProcessRequest) -> CapturedProcessResult:
        """Return a completed command or raise :class:`subprocess.TimeoutExpired`."""
        ...


class PopenProcess:
    """A ``subprocess.Popen`` started with :func:`start_process_group`."""

    def __init__(self, process: _TextProcess, *, stdin: str | None = None) -> None:
        """Wrap *process*, the leader of its own process group."""
        self._process = process
        self._stdin = stdin
        self._communication_started = False
        self._timeout_output: tuple[bytes | None, bytes | None] = (None, None)

    @property
    def returncode(self) -> int:
        """The return code of the exited leader."""
        code = self._process.returncode
        if code is None:
            message = "the process has not exited"
            raise RuntimeError(message)
        return code

    def wait(self, timeout: float | None) -> tuple[str, str] | None:
        """Wait with ``communicate``, retaining partial output after timeout."""
        try:
            stdin = None if self._communication_started else self._stdin
            self._communication_started = True
            return self._process.communicate(input=stdin, timeout=timeout)
        except ProcessTimeoutError as error:
            self._timeout_output = (error.output, error.stderr)
            return None

    @property
    def timeout_output(self) -> tuple[bytes | None, bytes | None]:
        """Return the raw partial streams reported by ``communicate``."""
        return self._timeout_output

    def signal_group(self, signal_number: StopSignal) -> None:
        """Signal the group; it outlives its leader while any descendant remains in it."""
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, signal_number)

    def signal_group_if_unreaped(self, signal_number: StopSignal) -> bool:
        """Signal while this handle still reserves the leader's PID and PGID."""
        # ``Popen.returncode`` changes only when this owner reaps the child.
        # A leader that exited in the kernel but remains unreaped is a zombie,
        # so its PID still reserves the process-group identity and is safe to
        # signal. Conversely, ``communicate`` can reap before raising a decode
        # error, in which case signalling the stale numeric PGID is unsafe.
        if self._process.returncode is not None:
            return False
        self.signal_group(signal_number)
        return True

    def close(self) -> None:
        """Release all pipes and reap an already-exited leader without blocking."""
        failures: list[BaseException] = []
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException as failure:  # noqa: BLE001  # lint-waiver: LW-194305 [BLE001]; all three owned pipes must be attempted even when cancellation or an injected stream failure interrupts an earlier close.
                    # > Catching only I/O errors would let cancellation skip later
                    # > resources; a context manager would retain only its last
                    # > failure instead of reporting the complete cleanup.
                    failures.append(failure)
        self._process.poll()
        raise_pipe_close_failures(failures)


def raise_pipe_close_failures(failures: list[BaseException]) -> None:
    """Raise one close failure unchanged or group several in attempt order."""
    if len(failures) == 1:
        raise failures[0]
    if failures:
        message = "captured process pipes could not be closed"
        raise BaseExceptionGroup(message, failures)


def _cleanup_failure_message(failure: BaseException) -> str:
    """Describe nested cleanup failures in their deterministic attempt order."""
    if isinstance(failure, BaseExceptionGroup):
        return "; ".join(_cleanup_failure_message(item) for item in failure.exceptions)
    return str(failure) or type(failure).__name__


def _open_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    cwd: str | None,
    stdin: int | None,
    start_new_session: bool = False,
) -> _TextProcess:
    return subprocess.Popen(  # noqa: S603  # lint-waiver: LW-731001 [S603]; the sandbox boundary intentionally executes its caller's command.
        # > Each sandbox assembles its own argv (a shell command is ``sh -c``);
        # > validating argv here would duplicate each sandbox's command contract.
        argv,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=start_new_session,
        process_group=-1 if start_new_session else 0,
    )


def start_process_group(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    cwd: str | None,
) -> _TextProcess:
    """Start *argv* as the leader of a new process group with piped output.

    Launch failures raise the ``subprocess`` error unchanged.
    """
    return _open_process(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL)


class _SubprocessCapturedProcessLauncher:
    """Start a real captured process group for :class:`OwnedProcessRunner`."""

    def start(self, request: CapturedProcessRequest) -> CapturedProcess:
        process = _open_process(
            request.argv,
            env=request.env,
            cwd=request.cwd,
            stdin=subprocess.PIPE if request.stdin is not None else None,
            start_new_session=request.start_new_session,
        )
        return PopenProcess(process, stdin=request.stdin)


class OwnedProcessRunner:
    """Run a captured command and settle its locally owned group on failure.

    A timeout sends ``SIGKILL`` to the group while its leader remains unreaped,
    preserving ``subprocess.run``'s immediate-kill policy while extending it to
    descendants. Other failures do the same only while the leader remains
    unreaped; after a decode failure has reaped it, cleanup cannot safely signal
    its potentially reused numeric PGID. Cleanup waits at most
    *settle_seconds*, then closes the captured pipes and reaps an exited leader
    without another blocking wait. The original exception remains authoritative.
    """

    def __init__(
        self,
        *,
        processes: CapturedProcessLauncher | None = None,
        threads: Threads | None = None,
        settle_seconds: float = DEFAULT_PROCESS_SETTLE_SECONDS,
    ) -> None:
        """Use OS process effects by default with a finite cleanup budget."""
        if not isfinite(settle_seconds) or settle_seconds < 0:
            message = "process settle timeout must be finite and nonnegative"
            raise ValueError(message)
        self._processes = processes or _SubprocessCapturedProcessLauncher()
        self._threads = threads or OsThreads()
        self._settle_seconds = settle_seconds

    def run(self, request: CapturedProcessRequest) -> CapturedProcessResult:
        """Run *request*, safely settling its owned process group on failure."""
        process = self._processes.start(request)
        try:
            finished = process.wait(request.timeout)
        except BaseException as failure:
            self._settle_owned_failure(process, failure)
            raise
        if finished is None:
            error = ProcessTimeoutError(request.argv, request.timeout)
            self._settle_owned_failure(process, error)
            error.output, error.stderr = process.timeout_output
            raise error
        try:
            return CapturedProcessResult(process.returncode, *finished)
        finally:
            process.close()

    def _settle_owned_failure(
        self,
        process: CapturedProcess,
        failure: BaseException,
    ) -> tuple[str, str] | None:
        """Kill and settle a group only while its leader identity is reserved."""
        settled: tuple[str, str] | None = None
        deadline = self._threads.now() + self._settle_seconds
        try:
            if process.signal_group_if_unreaped(KILL):
                settled = process.wait(max(0.0, deadline - self._threads.now()))
                if settled is None:
                    failure.add_note(
                        f"process group did not settle within {self._settle_seconds:g} seconds"
                    )
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-731022 [BLE001]; cleanup must preserve the caller's timeout or interruption while still attempting pipe closure.
            # > Catching only OS errors would let an injected process adapter replace
            # > the primary failure; a wrapper around these two steps adds no isolation.
            detail = _cleanup_failure_message(cleanup_error)
            failure.add_note(f"process group cleanup also failed: {detail}")
        try:
            process.close()
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-731023 [BLE001]; pipe cleanup must preserve the primary process failure, including interpreter-level cancellation.
            # > Narrow catches cannot guarantee closure after an injected adapter's
            # > failure, and a generic cleanup helper would obscure ownership here.
            detail = _cleanup_failure_message(cleanup_error)
            failure.add_note(f"process pipe cleanup also failed: {detail}")
        return settled


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
