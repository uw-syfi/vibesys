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
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from typing import IO


# Preserve the established stdlib exception type while keeping the concrete
# subprocess dependency behind the public process boundary.
ProcessTimeoutError = subprocess.TimeoutExpired
type PopenFactory = Callable[..., subprocess.Popen[bytes]]


@dataclass(frozen=True)
class ProcessSpec:
    """What to run: the argument vector, its working directory and environment, its input."""

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    input: bytes = b""

    def __post_init__(self) -> None:
        """Reject an empty command."""
        if not self.argv:
            message = "a process needs a program: argv is empty"
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


class MergedOutputProcess(Protocol):
    """A process-group leader whose stdout and stderr share one binary pipe."""

    def read(self, max_bytes: int) -> bytes:
        """Read at most *max_bytes* from the merged output, or empty bytes at EOF."""
        ...

    def wait(self) -> int:
        """Wait for the group leader and return its ``subprocess`` status."""
        ...

    def terminate_group(self) -> None:
        """Send SIGTERM to every process remaining in the owned group."""
        ...

    def kill_group(self) -> None:
        """Send SIGKILL to every process remaining in the owned group."""
        ...


class MergedOutputProcessLauncher(Protocol):
    """Start merged-binary-output commands as leaders of owned POSIX process groups."""

    def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None,
        env: Mapping[str, str],
    ) -> MergedOutputProcess:
        """Start *argv* with no input and one merged binary output pipe."""
        ...


class PipedProcess(Protocol):
    """A synchronous child that owns a POSIX process group and two text output pipes."""

    @property
    def stdout(self) -> io.TextIOBase:
        """The process's text stdout pipe."""
        ...

    @property
    def stderr(self) -> io.TextIOBase:
        """The process's text stderr pipe."""
        ...

    def wait(self, timeout: float | None) -> int:
        """Wait for the group leader, raising :class:`ProcessTimeoutError` on timeout."""
        ...

    def kill_group(self) -> None:
        """Kill the leader and every descendant remaining in its owned process group."""
        ...

    def cancel_output(self) -> None:
        """Cause active stdout and stderr reads to return promptly."""
        ...


class PipedProcessLauncher(Protocol):
    """Start a synchronous command with piped text output in an owned process group."""

    def start(self, argv: tuple[str, ...], *, cwd: Path | None = None) -> PipedProcess:
        """Start *argv* without a shell, raising ``OSError`` when it cannot start."""
        ...


class _StartedPipedProcess(Protocol):
    """A newly started group whose binary pipes have not changed ownership yet."""

    @property
    def stdout(self) -> IO[bytes]: ...

    @property
    def stderr(self) -> IO[bytes]: ...

    def wait(self, timeout: float | None) -> int: ...

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

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @property
    def stdout(self) -> IO[bytes]:
        stream = self._process.stdout
        if stream is None:
            message = "piped subprocess has no stdout stream"
            raise RuntimeError(message)
        return stream

    @property
    def stderr(self) -> IO[bytes]:
        stream = self._process.stderr
        if stream is None:
            message = "piped subprocess has no stderr stream"
            raise RuntimeError(message)
        return stream

    def wait(self, timeout: float | None) -> int:
        return self._process.wait(timeout=timeout)

    def kill_group(self) -> None:
        _signal_process_group(self._process.pid, signal.SIGKILL)


def _signal_process_group(pid: int, number: signal.Signals) -> None:
    """Signal a group that may already have finished between policy and delivery."""
    try:
        os.killpg(pid, number)
    except ProcessLookupError:
        return


class _SubprocessMergedOutputProcess:
    """The real merged-output process-group handle."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._reap_lock = threading.Lock()
        self._returncode: int | None = None

    def read(self, max_bytes: int) -> bytes:
        stdout = self._process.stdout
        if stdout is None:
            message = "merged-output subprocess has no stdout stream"
            raise RuntimeError(message)
        return os.read(stdout.fileno(), max_bytes)

    def wait(self) -> int:
        with self._reap_lock:
            if self._returncode is not None:
                return self._returncode
        self._wait_until_exited_without_reaping()
        with self._reap_lock:
            if self._returncode is None:
                self._returncode = self._process.wait()
            return self._returncode

    def terminate_group(self) -> None:
        self._signal_group(signal.SIGTERM)

    def kill_group(self) -> None:
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, number: signal.Signals) -> None:
        with self._reap_lock:
            if self._returncode is None:
                _signal_process_group(self._process.pid, number)

    def _wait_until_exited_without_reaping(self) -> None:
        """Keep the leader PID reserved until reaping and group signals serialize."""
        try:
            os.waitid(os.P_PID, self._process.pid, os.WEXITED | os.WNOWAIT)
        except ChildProcessError:
            with self._reap_lock:
                if self._returncode is not None:
                    return
            raise


class SubprocessMergedOutputProcessLauncher:
    """Launch real merged-output commands in process groups owned by the caller."""

    def __init__(self, popen: PopenFactory | None = None) -> None:
        """Use *popen* when supplied, preserving legacy process-factory injection."""
        self._popen = popen or subprocess.Popen

    def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None,
        env: Mapping[str, str],
    ) -> MergedOutputProcess:
        """Start *argv* without a shell, isolated from the caller's terminal group."""
        process = self._popen(
            tuple(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            process_group=0,
        )
        return _SubprocessMergedOutputProcess(process)


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
            stderr = open_output(process.stderr)
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
        cleanup.callback(process.stderr.close)
        cleanup.callback(process.stdout.close)


def _start_subprocess_group(argv: tuple[str, ...], cwd: Path | None) -> _StartedPipedProcess:
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-994699 [S603]; execute the caller's argv directly with no shell at the process adapter boundary.
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        process_group=0,
    )
    return _SubprocessGroup(process)


_START_ROLLBACK_TIMEOUT_S = 5.0


class SubprocessPipedProcessLauncher:
    """Start piped commands in owned groups on VibeSys's supported POSIX hosts."""

    def start(self, argv: tuple[str, ...], *, cwd: Path | None = None) -> PipedProcess:
        """Start *argv* in a new POSIX process group with UTF-8 text pipes."""
        if os.name != "posix":
            message = "piped process groups require a POSIX host"
            raise OSError(message)
        return _start_piped_process(argv, cwd, _start_subprocess_group, _CancellableTextPipe)


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


class _InheritedHandle:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def wait(self) -> int:
        return await self._process.wait()

    def send_signal(self, number: signal.Signals) -> None:
        if self._process.returncode is None:
            self._process.send_signal(number)


class InheritedStdioLauncher:
    """Starts real operating-system processes that use this process's terminal."""

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ForegroundChild:
        """Start ``argv`` on the running event loop with inherited standard streams."""
        process = await asyncio.create_subprocess_exec(
            *argv, env=None if env is None else dict(env), cwd=cwd
        )
        return _InheritedHandle(process)


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
