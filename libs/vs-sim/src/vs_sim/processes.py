"""Child processes behind an interface, so a test can script one on a virtual clock."""

from __future__ import annotations

import asyncio
import codecs
import io
import os
import selectors
import signal
import subprocess
import threading
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from vs_sim.output import Capture, OutputStream, tee

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableMapping, Sequence
    from pathlib import Path
    from typing import IO

    from vs_sim.exit_wait import ExitWaiter
    from vs_sim.output import OutputSink


# Preserve the established stdlib exception type while keeping the concrete
# subprocess dependency behind the public process boundary.
ProcessTimeoutError = subprocess.TimeoutExpired
_CLEANUP_TIMEOUT_S = 5.0
_PIPE_CHUNK_BYTES = 64 * 1024

DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024 * 1024
"""The most a run keeps of each output stream unless its spec sets a cap: 256 MiB."""


@dataclass(frozen=True)
class ProcessSpec:
    """What to run: the argument vector, its working directory and environment, its input.

    ``max_output_bytes`` caps each captured stream. :meth:`Processes.run` stops a command
    that writes more to either one and reports :class:`ProcessOutputTooLarge`.
    """

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    input: bytes = b""
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        """Reject an empty command and a negative output cap."""
        if not self.argv:
            message = "a process needs a program: argv is empty"
            raise ValueError(message)
        if self.max_output_bytes < 0:
            message = f"max_output_bytes must not be negative, got {self.max_output_bytes}"
            raise ValueError(message)


@dataclass(frozen=True)
class ProcessOutcome:
    """How a process ended. A signal death has a negative ``returncode``, as in ``subprocess``."""

    returncode: int
    stdout: bytes
    stderr: bytes


class RunningProcess(Protocol):
    """A started process."""

    async def wait(self) -> ProcessOutcome:
        """Wait for it to end, feeding its input and collecting its output."""
        ...

    def terminate(self) -> None:
        """Ask it to stop (SIGTERM); a no-op once it has ended."""
        ...

    def kill(self) -> None:
        """Make it stop (SIGKILL); a no-op once it has ended."""
        ...


class ProcessLauncher(Protocol):
    """Starts child processes."""

    async def start(self, spec: ProcessSpec) -> RunningProcess:
        """Start ``spec``; raises ``OSError`` when the program cannot be started."""
        ...


@dataclass(frozen=True)
class ProcessTimedOut:
    """The command outlived its timeout; its whole group was then killed and reaped."""

    timeout: float
    stdout: bytes
    """What the command wrote to stdout before it was killed."""
    stderr: bytes


@dataclass(frozen=True)
class ProcessGroupUnreaped:
    """The command outlived its timeout and killing its group did not end it.

    Either the leader did not exit within the cleanup bound after ``SIGKILL``, or a descendant
    that left the group still holds an output pipe open. Something the command started may
    still be running, so this is not a plain timeout.
    """

    timeout: float
    pid: int
    """The process ID of the group's leader."""
    detail: str
    """What failed, in words, for a log line or error message."""


@dataclass(frozen=True)
class ProcessOutputTooLarge:
    """The command wrote more than ``limit`` bytes to ``stream``; its whole group was then killed.

    The streams hold the first ``limit`` bytes of each. ``detail`` is empty when the kill
    ended the group, and otherwise says what may still be running, as for
    :class:`ProcessGroupUnreaped`.
    """

    limit: int
    stream: OutputStream
    stdout: bytes
    stderr: bytes
    detail: str = ""


type ProcessRun = ProcessOutcome | ProcessTimedOut | ProcessGroupUnreaped | ProcessOutputTooLarge
"""How a run ended: exited, timed out (cleaned up or left behind), or stopped for too much output."""


def forward_output(run: ProcessRun, output: OutputSink) -> ProcessRun:
    """Write what ``run`` kept of each stream to ``output``, and return ``run``.

    For a :class:`Processes` whose command's output arrives all at once (a broker's reply, a
    scripted answer) rather than chunk by chunk.
    """
    if not isinstance(run, ProcessGroupUnreaped):
        output.write(OutputStream.STDOUT, run.stdout)
        output.write(OutputStream.STDERR, run.stderr)
    return run


class Processes(Protocol):
    """Runs a command to completion: the blocking counterpart of :class:`ProcessLauncher`.

    The command runs without a shell in a process group of its own, with ``spec.input`` as its
    stdin and both output streams captured, each up to ``spec.max_output_bytes``. Raises
    ``OSError`` when the program cannot be started. A command that outlives ``timeout`` has
    its whole group killed; the result says whether that ended everything
    (:class:`ProcessTimedOut`) or not (:class:`ProcessGroupUnreaped`). A command that writes
    past the cap has its whole group killed too (:class:`ProcessOutputTooLarge`).

    Every chunk the command writes is also written to ``output`` as it is read, past the
    cap too, before ``run`` returns; the result is the same whatever ``output`` is.
    """

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        """Run ``spec`` until it exits or ``timeout`` seconds pass (``None``: no limit)."""
        ...


class OsProcesses:
    """Runs real operating-system processes, blocking the calling thread."""

    def __init__(self, *, cleanup_timeout: float = _CLEANUP_TIMEOUT_S) -> None:
        """Wait at most ``cleanup_timeout`` seconds for a killed group to release its output."""
        self._cleanup_timeout = cleanup_timeout

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        """Start ``spec`` as a group leader, feed its input, and collect and forward its output."""
        if os.name != "posix":
            message = "owned process groups require a POSIX host"
            raise OSError(message)
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-731024 [S603]; the real adapter runs a caller-assembled argv vector without a shell.
            # > Validating argv here would duplicate each caller's command contract, and a
            # > shell would add the quoting hazards this interface exists to avoid.
            spec.argv,
            cwd=spec.cwd,
            env=None if spec.env is None else dict(spec.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            process_group=0,
        )
        group = _OwnedProcessGroup(process)
        captured = Capture(spec.max_output_bytes)
        pipes = _Pipes(spec.input, captured, tee(captured, output))
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            end = _pump(process, deadline, pipes, feed=True)
        except BaseException as failure:
            if isinstance(detail := self._kill(process, group, pipes), str):
                failure.add_note(detail)
            raise
        if end is _PumpEnd.DONE:
            _close_pipes(process)
            return ProcessOutcome(group.reap(timeout=None), captured.stdout, captured.stderr)
        cleanup = self._kill(process, group, pipes)
        stdout, stderr = captured.stdout, captured.stderr
        if end is _PumpEnd.TIMEOUT:
            if cleanup:
                return ProcessGroupUnreaped(timeout or 0.0, process.pid, cleanup)
            return ProcessTimedOut(timeout or 0.0, stdout, stderr)
        stream = OutputStream.STDOUT if end is _PumpEnd.STDOUT_FULL else OutputStream.STDERR
        return ProcessOutputTooLarge(spec.max_output_bytes, stream, stdout, stderr, cleanup)

    def _kill(
        self, process: subprocess.Popen[bytes], group: _OwnedProcessGroup, pipes: _Pipes
    ) -> str:
        """Kill the group and drain its output within the cleanup bound; say what failed, if anything.

        A failed kill is reported even when the output then closes: the leader may have exited
        on its own while other members of its group, which the kill did not reach, live on.
        """
        problems: list[str] = []
        try:
            group.send_signal(signal.SIGKILL)
        except OSError as error:
            problems.append(f"killing process group {process.pid} failed: {error}")
        deadline = time.monotonic() + self._cleanup_timeout
        if _pump(process, deadline, pipes, feed=False) is _PumpEnd.DONE:
            _close_pipes(process)
            try:
                group.reap(timeout=self._cleanup_timeout)
            except subprocess.TimeoutExpired:
                problems.append(f"process {process.pid} was not reaped")
            return "; ".join(problems)
        problems.append(
            f"the output of process group {process.pid} did not close within "
            f"{self._cleanup_timeout:g} seconds of the kill"
        )
        _close_pipes(process)
        try:
            group.reap(timeout=self._cleanup_timeout)
        except subprocess.TimeoutExpired:
            problems.append(f"process {process.pid} was not reaped")
        return "; ".join(problems)


class _PumpEnd(StrEnum):
    DONE = "done"
    TIMEOUT = "timeout"
    STDOUT_FULL = "stdout_full"
    STDERR_FULL = "stderr_full"


class _Pipes:
    """One run's input still to write and where its output goes: kept under the cap, and on."""

    def __init__(self, data: bytes, captured: Capture, output: OutputSink) -> None:
        self.data = data
        self.captured = captured
        self.output = output
        self._input_offset = 0

    def write_input(self, fd: int) -> bool:
        """Write the next input chunk; ``True`` once all of it was written or the reader left."""
        try:
            self._input_offset += os.write(
                fd, self.data[self._input_offset : self._input_offset + _PIPE_CHUNK_BYTES]
            )
        except BrokenPipeError:
            return True
        return self._input_offset >= len(self.data)

    def pending_input(self) -> bytes:
        """The input not yet written."""
        return self.data[self._input_offset :]


def _pump(
    process: subprocess.Popen[bytes],
    deadline: float | None,
    pipes: _Pipes,
    *,
    feed: bool,
) -> _PumpEnd:
    """Read both outputs until they close, the deadline, or the cap; feed stdin when *feed*.

    Every chunk read goes to the pipes' output. While feeding, the first stream past the
    cap ends the pump; a drain after a kill (no *feed*) closes stdin and reads on past it.
    """
    with selectors.DefaultSelector() as selector:
        open_outputs = _register(selector, process, pipes.pending_input() if feed else b"")
        while open_outputs:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return _PumpEnd.TIMEOUT
            for key, _events in selector.select(remaining):
                if key.data is None:
                    if pipes.write_input(key.fd):
                        selector.unregister(key.fd)
                        _close_quietly(process.stdin)
                    continue
                chunk = os.read(key.fd, _PIPE_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fd)
                    open_outputs -= 1
                else:
                    pipes.output.write(key.data, chunk)
                    if feed and pipes.captured.omitted(key.data):
                        return _FULL[key.data]
    return _PumpEnd.DONE


_FULL = {OutputStream.STDOUT: _PumpEnd.STDOUT_FULL, OutputStream.STDERR: _PumpEnd.STDERR_FULL}


def _register(
    selector: selectors.BaseSelector, process: subprocess.Popen[bytes], data: bytes
) -> int:
    """Register the still-open pipes; return how many output pipes are open."""
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    if stdout is None or stderr is None:
        message = "a captured process needs both output pipes"
        raise RuntimeError(message)
    if stdin is not None and not stdin.closed:
        if data:
            os.set_blocking(stdin.fileno(), False)
            selector.register(stdin.fileno(), selectors.EVENT_WRITE, None)
        else:
            _close_quietly(stdin)
    open_outputs = 0
    for pipe, stream in ((stdout, OutputStream.STDOUT), (stderr, OutputStream.STDERR)):
        if not pipe.closed:
            selector.register(pipe.fileno(), selectors.EVENT_READ, stream)
            open_outputs += 1
    return open_outputs


def _close_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        _close_quietly(stream)


def _close_quietly(stream: IO[bytes] | None) -> None:
    if stream is not None:
        with suppress(OSError):
            stream.close()


class PipedProcess(Protocol):
    """A synchronous child that owns a POSIX process group and two text output pipes.

    Signals reach the group only while its leader is unreaped, so a group whose
    numeric ID the kernel may already have reused is never signalled.
    """

    @property
    def stdout(self) -> io.TextIOBase:
        """The process's text stdout pipe (stdout and stderr when started merged).

        ``readline`` returns one line; ``read(size)`` returns as soon as any
        output is available, at most *size* characters, and ``""`` at EOF.
        """
        ...

    @property
    def stderr(self) -> io.TextIOBase:
        """The process's text stderr pipe, already at EOF when started merged."""
        ...

    def wait(self, timeout: float | None) -> int:
        """Wait for the group leader, raising :class:`ProcessTimeoutError` on timeout."""
        ...

    def terminate_group(self) -> None:
        """Send SIGTERM to the leader and every descendant remaining in its group."""
        ...

    def kill_group(self) -> None:
        """Kill the leader and every descendant remaining in its owned process group."""
        ...

    def cancel_output(self) -> None:
        """Cause active stdout and stderr reads to return promptly."""
        ...


class PipedProcessLauncher(Protocol):
    """Start a synchronous command with piped text output in an owned process group."""

    def start(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        merge_stderr: bool = False,
    ) -> PipedProcess:
        """Start *argv* without a shell or input, raising ``OSError`` when it cannot start.

        *env* replaces the caller's environment when given. With
        *merge_stderr* the child's stderr shares its stdout pipe, so the two
        interleave in the order the child wrote them.
        """
        ...


class _StartedPipedProcess(Protocol):
    """A newly started group whose binary pipes have not changed ownership yet."""

    @property
    def stdout(self) -> IO[bytes]: ...

    @property
    def stderr(self) -> IO[bytes] | None: ...

    def wait(self, timeout: float | None) -> int: ...

    def terminate_group(self) -> None: ...

    def kill_group(self) -> None: ...


@runtime_checkable
class _CancellableOutput(Protocol):
    def cancel(self) -> None: ...


class _CancellableTextPipe(io.TextIOBase):
    """Decode one pipe while a private descriptor can interrupt a blocked read."""

    _READ_SIZE = 64 * 1024

    def __init__(self, stream: IO[bytes]) -> None:
        super().__init__()
        self._stream: IO[bytes] | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._decoded = ""
        self._eof = False
        self._cancel_reader = -1
        self._cancel_writer = -1
        self._selector: selectors.BaseSelector | None = None

        cancel_reader = -1
        cancel_writer = -1
        selector: selectors.BaseSelector | None = None
        try:
            cancel_reader, cancel_writer = os.pipe()
            os.set_blocking(cancel_writer, False)
            selector = selectors.DefaultSelector()
            selector.register(stream.fileno(), selectors.EVENT_READ, "output")
            selector.register(cancel_reader, selectors.EVENT_READ, "cancel")
        except BaseException as construction_error:
            try:
                _close_pipe_resources(selector, cancel_reader, cancel_writer)
            except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-994698 [BLE001]; cancellation and interpreter-exit exceptions must not leak descriptors, and the wrapper-construction failure remains the primary error.
                construction_error.add_note(f"output pipe cleanup also failed: {cleanup_error}")
            raise

        self._stream = stream
        self._cancel_reader = cancel_reader
        self._cancel_writer = cancel_writer
        self._selector = selector

    def readable(self) -> bool:
        """Report the only operation this process pipe supports."""
        return True

    def readline(self, size: int = -1, /) -> str:
        """Return one decoded line, or EOF once cancellation is requested."""
        if self.closed:
            raise _closed_pipe_error()
        while True:
            newline = self._decoded.find("\n")
            boundary = newline + 1 if newline >= 0 else -1
            if size >= 0 and (boundary < 0 or size < boundary) and len(self._decoded) >= size:
                boundary = size
            if boundary >= 0:
                line, self._decoded = self._decoded[:boundary], self._decoded[boundary:]
                return line
            if self._eof:
                line, self._decoded = self._decoded, ""
                return line
            self._read_available()

    def read(self, size: int | None = -1, /) -> str:
        """Return what is available, at most *size* characters, waiting only for the first.

        A negative or missing *size* reads to EOF. ``""`` means EOF (or a
        cancelled read).
        """
        if self.closed:
            raise _closed_pipe_error()
        if size is None or size < 0:
            while not self._eof:
                self._read_available()
            text, self._decoded = self._decoded, ""
            return text
        while size and not self._decoded and not self._eof:
            self._read_available()
        text, self._decoded = self._decoded[:size], self._decoded[size:]
        return text

    def cancel(self) -> None:
        """Wake an active read without waiting for the child to close its pipe."""
        if self.closed or self._cancel_writer < 0:
            return
        with suppress(BlockingIOError, OSError):
            os.write(self._cancel_writer, b"\0")

    def close(self) -> None:
        """Release the output and cancellation descriptors once reading has stopped."""
        if self.closed:
            return
        selector, self._selector = self._selector, None
        stream, self._stream = self._stream, None
        cancel_reader, self._cancel_reader = self._cancel_reader, -1
        cancel_writer, self._cancel_writer = self._cancel_writer, -1
        with ExitStack() as cleanup:
            cleanup.callback(io.TextIOBase.close, self)
            cleanup.callback(_close_pipe_resources, selector, cancel_reader, cancel_writer)
            if stream is not None:
                cleanup.callback(stream.close)

    def _read_available(self) -> None:
        selector = self._selector
        stream = self._stream
        if selector is None or stream is None:
            raise _closed_pipe_error()
        events = selector.select()
        if any(key.data == "cancel" for key, _mask in events):
            self._decoded = ""
            self._eof = True
            return
        chunk = os.read(stream.fileno(), self._READ_SIZE)
        if chunk:
            self._decoded += self._decoder.decode(chunk)
            return
        self._decoded += self._decoder.decode(b"", final=True)
        self._eof = True


class _ExhaustedTextPipe(io.TextIOBase):
    """The stderr of a merged-output process, which shares the stdout pipe: always at EOF."""

    def readable(self) -> bool:
        """Report the only operation this pipe supports."""
        return True

    def readline(self, size: int = -1, /) -> str:
        """Return EOF."""
        del size
        if self.closed:
            raise _closed_pipe_error()
        return ""

    def read(self, size: int | None = -1, /) -> str:
        """Return EOF."""
        return self.readline(-1 if size is None else size)

    def cancel(self) -> None:
        """Nothing can block on this pipe."""


def _closed_pipe_error() -> ValueError:
    return ValueError("I/O operation on closed file")


def _close_pipe_resources(
    selector: selectors.BaseSelector | None,
    cancel_reader: int,
    cancel_writer: int,
) -> None:
    """Close every descriptor acquired while constructing a cancellable pipe."""
    with ExitStack() as cleanup:
        if cancel_writer >= 0:
            cleanup.callback(os.close, cancel_writer)
        if cancel_reader >= 0:
            cleanup.callback(os.close, cancel_reader)
        if selector is not None:
            cleanup.callback(selector.close)


class _SubprocessGroup:
    """A just-started POSIX process group whose binary pipes still belong to its launcher."""

    def __init__(self, process: subprocess.Popen[bytes], exit_waiter: ExitWaiter | None) -> None:
        self._process = process
        self._group = _OwnedProcessGroup(process, exit_waiter)

    @property
    def stdout(self) -> IO[bytes]:
        stream = self._process.stdout
        if stream is None:
            message = "piped subprocess has no stdout stream"
            raise RuntimeError(message)
        return stream

    @property
    def stderr(self) -> IO[bytes] | None:
        return self._process.stderr

    def wait(self, timeout: float | None) -> int:
        returncode = self._group.wait(timeout)
        if returncode is None:
            raise ProcessTimeoutError(self._process.args, timeout or 0.0)
        return returncode

    def terminate_group(self) -> None:
        self._group.send_signal(signal.SIGTERM)

    def kill_group(self) -> None:
        self._group.send_signal(signal.SIGKILL)


def _signal_process_group(pid: int, number: signal.Signals) -> None:
    """Signal a group that may already have finished between policy and delivery."""
    try:
        os.killpg(pid, number)
    except ProcessLookupError:
        return


def _host_exit_waiter() -> ExitWaiter:
    """The host's exit waiter, loading ``vs_sim.exit_wait`` when a group first needs one."""
    from vs_sim.exit_wait import (  # noqa: PLC0415  # lint-waiver: LW-731051 [PLC0415]; the host helper's hello path imports this module and its test caps the modules hello loads, so the waiter's module loads when a group first needs it; a top-level import adds a module to every hello, and importlib would hide the declared dependency from tach.
        host_exit_waiter,
    )

    return host_exit_waiter()


def _group_has_live_member(pgid: int) -> bool:
    """Whether the host's process table lists a live member in group ``pgid``.

    Raises ``OSError`` when the table cannot be read.
    """
    from vs_sim.process_table import (  # noqa: PLC0415  # lint-waiver: LW-731052 [PLC0415]; the same hello-path module cap as the exit waiter's import: only a refused group signal reads the process table, so its module loads then.
        ProcessTableUnavailableError,
        group_running,
    )

    try:
        return group_running(pgid)
    except ProcessTableUnavailableError as unavailable:
        raise OSError(str(unavailable)) from unavailable


class _ProcessGroupLeader(Protocol):
    """The identity and wait surface shared by text and binary ``Popen`` handles."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class _WatcherStoppedError(RuntimeError):
    """The thread watching a process group's leader stopped before the leader exited."""

    def __init__(self) -> None:
        """Say that the leader's exit was never observed."""
        super().__init__("the exit watcher stopped before the process group's leader exited")


class _OwnedProcessGroup:
    """Serialize process-group signals with leader reaping while its PGID is reserved."""

    def __init__(self, process: _ProcessGroupLeader, exit_waiter: ExitWaiter | None = None) -> None:
        """Own ``process``'s group; *exit_waiter* ``None`` chooses the host's when first needed."""
        self._process = process
        self._exit_waiter = exit_waiter
        self._reap_lock = threading.Lock()
        self._returncode: int | None = None
        self._exit_watch_lock = threading.Lock()
        self._exited: threading.Event | None = None
        self._watch_failure: Exception | None = None

    def wait(self, timeout: float | None) -> int | None:
        """Wait without reaping until a signal can no longer race PGID reuse.

        Returns ``None`` when the leader is still running after *timeout*
        seconds. A concurrent signal is never held back by a wait in progress.
        A watch that failed raises its failure here and is never reported as an
        exit, which would block in the reap with the leader still running.
        """
        with self._reap_lock:
            if self._returncode is not None:
                return self._returncode
        if timeout is None:
            self._wait_until_exited_without_reaping()
        elif not self._exit_watch().wait(timeout):
            return None
        elif (failure := self._watch_failure) is not None:
            raise failure
        return self.reap(timeout=None)

    def _exit_watch(self) -> threading.Event:
        """Return an event set once the leader exited, watched by one thread at most."""
        with self._exit_watch_lock:
            if self._exited is None:
                exited = threading.Event()

                def watch() -> None:
                    # Anything but a normal return leaves a failure, so a watcher that
                    # dies is never read as the leader's exit.
                    failure: Exception | None = _WatcherStoppedError()
                    try:
                        self._wait_until_exited_without_reaping()
                        failure = None
                    except ChildProcessError:
                        failure = None  # The leader is gone, so there is nothing to wait for.
                    except OSError as error:
                        failure = error
                    except BaseException as error:
                        failure.__cause__ = error  # The thread's excepthook prints it too.
                        raise
                    finally:
                        self._watch_failure = failure
                        exited.set()

                threading.Thread(target=watch, name="vs-sim-group-exit", daemon=True).start()
                self._exited = exited
            return self._exited

    def reap(self, timeout: float | None) -> int:
        """Reap the leader once while excluding every process-group signal."""
        with self._reap_lock:
            if self._returncode is None:
                self._returncode = self._process.wait(timeout=timeout)
            return self._returncode

    def send_signal(self, number: signal.Signals) -> None:
        """Signal only while this owner still reserves the leader's PID/PGID."""
        with self._reap_lock:
            if self._returncode is not None:
                return
            # ``Popen.communicate`` normally leaves a timed-out child unreaped,
            # but another failure (for example decoding after normal exit) may
            # occur after it reaped the leader itself. Never signal that stale
            # numeric PGID, which the kernel is then free to reuse.
            observed_returncode = self._process.returncode
            if observed_returncode is not None:
                self._returncode = observed_returncode
                return
            try:
                _signal_process_group(self._process.pid, number)
            except PermissionError:
                if not self._nothing_live_is_left():
                    raise

    def _nothing_live_is_left(self) -> bool:
        """Whether a refused group signal was refused only because nothing live is left to signal.

        macOS answers ``EPERM`` where Linux succeeds when every member of the group is an
        unreaped zombie. A live member this user cannot signal (a setuid child) is refused the
        same way and must still be reported, so the refusal is forgiven only when the leader
        has exited and the host's process table lists no live member. If either cannot be
        told, nothing is forgiven.
        """
        pid = self._process.pid
        try:
            return self._waiter().has_exited(pid) and not _group_has_live_member(pid)
        except OSError:
            return False

    def _waiter(self) -> ExitWaiter:
        """The exit waiter: the one given, else the host's, chosen on first use."""
        waiter = self._exit_waiter
        if waiter is None:
            waiter = self._exit_waiter = _host_exit_waiter()
        return waiter

    def _wait_until_exited_without_reaping(self) -> None:
        try:
            self._waiter().wait(self._process.pid)
        except ChildProcessError:
            with self._reap_lock:
                if self._returncode is not None:
                    return
            raise


class _SubprocessPipedProcess:
    def __init__(
        self,
        process: _StartedPipedProcess,
        open_output: Callable[[IO[bytes]], io.TextIOBase],
    ) -> None:
        self._process = process
        ownership = ExitStack()
        ownership.callback(_close_process_output, process)
        try:
            stdout = open_output(process.stdout)
            ownership.callback(stdout.close)
            _require_cancellable(stdout)
            raw_stderr = process.stderr
            stderr = _ExhaustedTextPipe() if raw_stderr is None else open_output(raw_stderr)
            ownership.callback(stderr.close)
            _require_cancellable(stderr)
        except BaseException as construction_error:
            try:
                ownership.close()
            except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-010103 [BLE001]; every partially built wrapper must close on cancellation or interpreter exit without replacing the construction failure.
                construction_error.add_note(f"output wrapper cleanup also failed: {cleanup_error}")
            raise
        ownership.pop_all()
        self._stdout = stdout
        self._stderr = stderr

    @property
    def stdout(self) -> io.TextIOBase:
        return self._stdout

    @property
    def stderr(self) -> io.TextIOBase:
        return self._stderr

    def wait(self, timeout: float | None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate_group(self) -> None:
        self._process.terminate_group()

    def kill_group(self) -> None:
        self._process.kill_group()

    def cancel_output(self) -> None:
        """Interrupt both output readers independently of process-group signalling."""
        cast("_CancellableOutput", self._stdout).cancel()
        cast("_CancellableOutput", self._stderr).cancel()


def _require_cancellable(stream: io.TextIOBase) -> None:
    if not isinstance(stream, _CancellableOutput):
        message = "piped process output must support cancellation"
        raise TypeError(message)


def _close_process_output(process: _StartedPipedProcess) -> None:
    """Close both raw pipes even when closing either one fails."""
    with ExitStack() as cleanup:
        stderr = process.stderr
        if stderr is not None:
            cleanup.callback(stderr.close)
        cleanup.callback(process.stdout.close)


def _start_subprocess_group(
    argv: tuple[str, ...],
    cwd: Path | None,
    *,
    env: Mapping[str, str] | None,
    merge_stderr: bool,
    exit_waiter: ExitWaiter | None,
) -> _StartedPipedProcess:
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-994699 [S603]; execute the caller's argv directly with no shell at the process adapter boundary.
        argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        process_group=0,
    )
    return _SubprocessGroup(process, exit_waiter)


_START_ROLLBACK_TIMEOUT_S = 5.0


class SubprocessPipedProcessLauncher:
    """Start piped commands in owned groups on VibeSys's supported POSIX hosts."""

    def __init__(self, *, exit_waiter: ExitWaiter | None = None) -> None:
        """Wait for a leader's exit with *exit_waiter*, by default the host's own mechanism.

        The default is chosen when a group first needs it, so building a launcher loads nothing.
        """
        self._exit_waiter = exit_waiter

    def start(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        merge_stderr: bool = False,
    ) -> PipedProcess:
        """Start *argv* in a new POSIX process group with UTF-8 text pipes."""
        if os.name != "posix":
            message = "piped process groups require a POSIX host"
            raise OSError(message)
        start = partial(
            _start_subprocess_group,
            env=env,
            merge_stderr=merge_stderr,
            exit_waiter=self._exit_waiter,
        )
        return _start_piped_process(argv, cwd, start, _CancellableTextPipe)


def _start_piped_process(
    argv: tuple[str, ...],
    cwd: Path | None,
    start_process: Callable[[tuple[str, ...], Path | None], _StartedPipedProcess],
    open_output: Callable[[IO[bytes]], io.TextIOBase],
) -> PipedProcess:
    """Construct one owned process behind an internal deterministic fault seam."""
    process = start_process(argv, cwd)
    try:
        return _SubprocessPipedProcess(process, open_output)
    except BaseException as construction_error:
        _rollback_failed_process_start(process, construction_error)
        raise


def _rollback_failed_process_start(
    process: _StartedPipedProcess,
    construction_error: BaseException,
) -> None:
    """Settle a group whose public wrapper could not take ownership."""
    cleanup_steps: tuple[tuple[str, Callable[[], object]], ...] = (
        ("kill process group", process.kill_group),
        ("reap process group", lambda: process.wait(timeout=_START_ROLLBACK_TIMEOUT_S)),
        ("close process output", lambda: _close_process_output(process)),
    )
    for description, cleanup in cleanup_steps:
        try:
            cleanup()
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-010104 [BLE001]; all rollback steps must run for every failure kind while the wrapper-construction error remains primary.
            construction_error.add_note(f"could not {description}: {cleanup_error}")


class _SubprocessHandle:
    def __init__(self, process: asyncio.subprocess.Process, data: bytes) -> None:
        self._process = process
        self._input = data
        self._outcome: ProcessOutcome | None = None

    async def wait(self) -> ProcessOutcome:
        if self._outcome is None:
            stdout, stderr = await self._process.communicate(self._input)
            returncode = await self._process.wait()
            self._outcome = ProcessOutcome(returncode, stdout, stderr)
        return self._outcome

    def terminate(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process.returncode is None:
            self._process.kill()


class SubprocessLauncher:
    """Starts real operating-system processes on the running event loop."""

    async def start(self, spec: ProcessSpec) -> RunningProcess:
        """Start ``spec`` with piped input and output."""
        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=spec.cwd,
            env=None if spec.env is None else dict(spec.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return _SubprocessHandle(process, spec.input)


class ForegroundChild(Protocol):
    """A started child that shares this process's terminal."""

    async def wait(self) -> int:
        """Wait for it to end; its exit status, negative when a signal ended it."""
        ...

    def send_signal(self, number: signal.Signals) -> None:
        """Deliver ``number`` to it; a no-op once it has ended."""
        ...


class ForegroundLauncher(Protocol):
    """Starts children that inherit stdin, stdout and stderr, as a shell's foreground job does."""

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ForegroundChild:
        """Start ``argv`` (in this process's environment and directory when ``None``).

        Raises ``OSError`` when the program cannot be started.
        """
        ...


LAUNCHER_LIFELINE_ENV = "VIBESYS_LAUNCHER_LIFELINE"
"""Names the descriptor of a child's launcher lifeline, set by ``InheritedStdioLauncher``.

The child holds the read end of a pipe whose only write end its launcher holds, so the
read reaches end-of-file exactly when the launcher has exited, however it ended
(``follow_launcher``)."""


class _InheritedHandle:
    def __init__(self, process: asyncio.subprocess.Process, lifeline: int) -> None:
        self._process = process
        self._lifeline: int | None = lifeline

    async def wait(self) -> int:
        try:
            return await self._process.wait()
        finally:
            # The child has ended; the lifeline has nobody left to tell.
            if self._lifeline is not None:
                os.close(self._lifeline)
                self._lifeline = None

    def send_signal(self, number: signal.Signals) -> None:
        if self._process.returncode is None:
            self._process.send_signal(number)


class InheritedStdioLauncher:
    """Starts real operating-system processes that use this process's terminal.

    Each child leads a session of its own, with no controlling terminal, so the
    terminal's job-control signals (interrupt, quit, suspend) reach only this process:
    the parent decides what reaches the child, and a child never gets one keypress twice,
    once from the terminal and once forwarded. The child still writes to the terminal
    through the descriptors it inherits, and job control never stops it for that (under
    ``stty tostop``) nor for setting the terminal's modes; the terminal is not its
    controlling terminal. Nor can it or any process it starts open ``/dev/tty``: a program
    that would prompt there (``ssh`` asking for a password or a host key) fails at once
    instead of turning the terminal's echo off under the operator, or stopping the run.

    Outside the terminal's session, the child no longer hears the terminal hang up. So
    each child gets a lifeline (``LAUNCHER_LIFELINE_ENV``): a child that calls
    ``follow_launcher`` hangs up when this process exits, by any means.
    """

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ForegroundChild:
        """Start ``argv`` on the running event loop with inherited standard streams."""
        given, held = os.pipe()
        environment = {**(os.environ if env is None else env), LAUNCHER_LIFELINE_ENV: str(given)}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, env=environment, cwd=cwd, start_new_session=True, pass_fds=(given,)
            )
        except BaseException:
            os.close(held)
            raise
        finally:
            os.close(given)
        return _InheritedHandle(process, held)


class LauncherLifelineError(ValueError):
    """``LAUNCHER_LIFELINE_ENV`` does not name a file descriptor."""

    def __init__(self, value: str) -> None:
        """Name the variable and the value it held."""
        super().__init__(f"{LAUNCHER_LIFELINE_ENV} must be a file descriptor number, not {value!r}")


def follow_launcher(environ: MutableMapping[str, str]) -> None:
    """Act on this process's launcher lifeline, if ``InheritedStdioLauncher`` gave it one.

    Call once, early, on the main thread of a launched child; ``environ`` is the process
    environment. Without a lifeline this does nothing. With one, it takes the lifeline out
    of ``environ`` and closes it on every ``exec`` (so no process the child starts reads it
    as its own, or holds it open), and hangs up when the launcher exits: once the lifeline
    reaches end-of-file, or cannot be read at all, this process sends itself SIGHUP, the
    signal a closed terminal sent before the child led its own session, so the child's own
    SIGHUP handling (a stop that cancels the run's work) runs even after a SIGKILL, OOM
    kill or crash of the launcher. A pipe works on every Unix; Linux's
    ``PR_SET_PDEATHSIG`` would not, and it fires when the parent *thread* exits, not the
    process.

    Raises ``LauncherLifelineError`` when ``LAUNCHER_LIFELINE_ENV`` does not hold a
    descriptor number.
    """
    value = environ.pop(LAUNCHER_LIFELINE_ENV, None)
    if value is None:
        return
    if not value.isdigit():
        raise LauncherLifelineError(value)
    lifeline = int(value)
    inheritable = False  # os.set_inheritable takes its flag by position only
    with suppress(OSError):
        # A descriptor that is not open is not read either: the watcher hangs up at once.
        os.set_inheritable(lifeline, inheritable)
    threading.Thread(
        target=_hang_up_at_end, args=(lifeline,), name="launcher-lifeline", daemon=True
    ).start()


def _hang_up_at_end(lifeline: int) -> None:
    """Block until the launcher exits, then hang this process up.

    A lifeline that cannot be read (or closed) hangs up too: the launcher can no longer
    be heard, and a run nobody can stop must not go on.
    """
    with suppress(OSError):
        # The launcher never writes; the read returns only at end-of-file.
        while os.read(lifeline, 1):
            pass
    with suppress(OSError):
        os.close(lifeline)
    os.kill(os.getpid(), signal.SIGHUP)


async def run_foreground(
    launcher: ForegroundLauncher,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    """Start ``argv`` on ``launcher`` and wait for it; its exit status."""
    child = await launcher.start(argv, env, cwd)
    return await child.wait()
