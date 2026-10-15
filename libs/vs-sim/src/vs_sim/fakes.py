"""In-memory Fakes of the vs_sim interfaces, for tests and simulations."""

from __future__ import annotations

import asyncio
import signal
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vs_sim.gate import arrival
from vs_sim.loopback_http import HttpReply
from vs_sim.processes import (
    ForegroundChild,
    OutputStream,
    PipedProcess,
    ProcessGroupUnreaped,
    ProcessOutcome,
    ProcessOutputTooLarge,
    ProcessSpec,
    ProcessTimedOut,
    RunningProcess,
)
from vs_sim.states import Changes, wait_for_state

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vs_sim.clock import Sleeper
    from vs_sim.probes import ProbeResult
    from vs_sim.processes import ProcessRun


class InlineBlockingRunner:
    """Runs the blocking function on the caller's thread, so the simulator stays single-threaded.

    The function runs to completion before anything else is scheduled, which is the one
    interleaving a simulation can order deterministically.
    """

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Call ``function`` now."""
        return function(*args, **kwargs)


class GatedBlockingRunner:
    """Runs each blocking call inline, but only once the test lets it start.

    A call that reaches :meth:`run` while the runner is held parks, in flight, until
    :meth:`release`; it then runs to completion on the caller's thread. That models an
    operation a test keeps "running" (to cancel its caller, start a second one, or close
    its owner) without a thread, so the loop stays single-threaded and deterministic.
    ``in_flight`` and ``max_in_flight`` count calls between entering and leaving
    :meth:`run`, which is how a test shows that two calls never overlapped.

    Cancelling a parked caller abandons its call without running it; code that must
    outlive its caller's cancellation (as a thread does) runs the call in its own task.
    """

    def __init__(self, *, held: bool = False) -> None:
        """Start held (every call parks) or open (every call runs at once)."""
        self._held = held
        self._parked: list[asyncio.Future[None]] = []
        self._watchers: list[tuple[int, asyncio.Future[None]]] = []
        self._in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    @property
    def in_flight(self) -> int:
        """Calls that have entered :meth:`run` and not yet left it."""
        return self._in_flight

    def hold(self) -> None:
        """Park every call that arrives from now on."""
        self._held = True

    def release(self) -> None:
        """Let parked calls and every later call run."""
        self._held = False
        parked, self._parked = self._parked, []
        for future in parked:
            if not future.done():
                future.set_result(None)

    async def wait_in_flight(self, count: int = 1, *operations: asyncio.Future[Any]) -> None:
        """Return once ``count`` calls are in flight, or fail with the outcome of an operation that ends first.

        Raises:
            AssertionError: an operation returned before ``count`` calls were in flight.
            BaseException: whatever an operation raised before then.
        """
        if self._in_flight >= count:
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (count, future)
        self._watchers.append(entry)
        try:
            await arrival(future, *operations)
        finally:
            if entry in self._watchers:
                self._watchers.remove(entry)

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Park while held, then call ``function`` now."""
        self.calls += 1
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        for needed, watcher in tuple(self._watchers):
            if self._in_flight >= needed and not watcher.done():
                watcher.set_result(None)
        try:
            while self._held:
                parked: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self._parked.append(parked)
                try:
                    await parked
                finally:
                    if parked in self._parked:
                        self._parked.remove(parked)
            return function(*args, **kwargs)
        finally:
            self._in_flight -= 1


class FakeSignalSource:
    """Signal handlers a test fires by hand."""

    def __init__(self) -> None:
        """Start with no handlers."""
        self._handlers: dict[signal.Signals, Callable[[], None]] = {}

    def add_handler(self, number: signal.Signals, handler: Callable[[], None]) -> None:
        """Install ``handler``, replacing an earlier one for ``number``."""
        self._handlers[number] = handler

    def remove_handler(self, number: signal.Signals) -> bool:
        """Remove the handler for ``number``; whether there was one."""
        return self._handlers.pop(number, None) is not None

    def handles(self, number: signal.Signals) -> bool:
        """Whether a handler is installed for ``number``."""
        return number in self._handlers

    def deliver(self, number: signal.Signals) -> bool:
        """Run the handler for ``number`` as the loop would; whether one was installed."""
        handler = self._handlers.get(number)
        if handler is None:
            return False
        handler()
        return True


class FakeProcessSignaller:
    """Stable process signalling over an in-memory set of live process IDs."""

    def __init__(self, live_pids: set[int]) -> None:
        """Treat exactly ``live_pids`` as openable process identities."""
        self.live_pids = set(live_pids)
        self.opened: list[int] = []
        self.terminated: list[int] = []

    def exists(self, pid: int) -> bool:
        """Whether ``pid`` is one of the live ids."""
        return pid in self.live_pids

    def terminate_if_current(self, pid: int, current: Callable[[], bool]) -> bool:
        """Open ``pid``, evaluate ``current``, and record a permitted termination."""
        if pid not in self.live_pids:
            raise ProcessLookupError(pid)
        self.opened.append(pid)
        if not current():
            return False
        self.terminated.append(pid)
        self.live_pids.remove(pid)
        return True


@dataclass(frozen=True)
class ProcessScript:
    """What a scripted process does: run for ``duration_s`` on the clock, then end like this."""

    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    duration_s: float = 0.0
    echo_input: bool = False
    """Make stdout the process's input (a ``cat``), after ``stdout``."""
    runs_until_signalled: bool = False
    """Never ends by itself: only ``terminate`` or ``kill`` ends it."""
    survives_kill: bool = False
    """Something it started outlives a kill of its group, so a run that times out leaves it."""


class _ScriptedProcess:
    def __init__(self, spec: ProcessSpec, script: ProcessScript, clock: Sleeper) -> None:
        self._spec = spec
        self._script = script
        self._clock = clock
        self._signalled: signal.Signals | None = None
        self._stopped = asyncio.Event()
        self._outcome: ProcessOutcome | None = None

    async def wait(self) -> ProcessOutcome:
        if self._outcome is None:
            self._outcome = await self._finish()
        return self._outcome

    async def _finish(self) -> ProcessOutcome:
        if not self._script.runs_until_signalled and self._signalled is None:
            sleeping = asyncio.ensure_future(self._clock.sleep(self._script.duration_s))
            stopped = asyncio.ensure_future(self._stopped.wait())
            try:
                await asyncio.wait({sleeping, stopped}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleeping.cancel()
                stopped.cancel()
        else:
            await self._stopped.wait()
        if self._signalled is not None:
            return ProcessOutcome(-self._signalled, b"", b"")
        stdout = self._script.stdout + (self._spec.input if self._script.echo_input else b"")
        return ProcessOutcome(self._script.returncode, stdout, self._script.stderr)

    def _stop(self, number: signal.Signals) -> None:
        if self._outcome is None and self._signalled is None:
            self._signalled = number
        self._stopped.set()

    def terminate(self) -> None:
        self._stop(signal.SIGTERM)

    def kill(self) -> None:
        self._stop(signal.SIGKILL)


@dataclass
class FakeProcessLauncher:
    """Starts scripted processes whose running time passes on the injected clock."""

    clock: Sleeper
    script: Callable[[ProcessSpec], ProcessScript]
    started: list[ProcessSpec] = field(default_factory=list)

    async def start(self, spec: ProcessSpec) -> RunningProcess:
        """Record ``spec`` and start the process the script describes for it."""
        self.started.append(spec)
        return _ScriptedProcess(spec, self.script(spec), self.clock)


def _too_large(limit: int, stdout: bytes, stderr: bytes) -> ProcessOutputTooLarge | None:
    """The outcome of a scripted run that writes past *limit*, stopped as the real one is."""
    for stream, written in ((OutputStream.STDOUT, stdout), (OutputStream.STDERR, stderr)):
        if len(written) > limit:
            return ProcessOutputTooLarge(limit, stream, stdout[:limit], stderr[:limit])
    return None


@dataclass
class FakeProcesses:
    """Runs scripted commands to completion; records every spec it was asked to run.

    The script describes each command (:class:`ProcessScript`) or raises the ``OSError`` a
    program that cannot start raises. Each run takes its time on ``pass_time``: its
    ``duration_s``, or the whole timeout when it outlives it. Pass ``threads.sleep`` in a
    simulation, or a manual clock's ``advance``, so a caller's deadline sees the time its
    commands took. A command whose ``duration_s`` exceeds the timeout, or one that
    ``runs_until_signalled``, times out as the real group would after being killed, and one
    that ``survives_kill`` is left unreaped. A never-ending command without a timeout would
    hang the caller, so the Fake refuses it.
    """

    script: Callable[[ProcessSpec], ProcessScript]
    pass_time: Callable[[float], None]
    """Lets the given number of seconds pass for the caller, as a real run would take them."""
    ran: list[ProcessSpec] = field(default_factory=list)
    first_pid: int = 1000
    """The process ID of the first run; each later run gets the next one."""

    def run(self, spec: ProcessSpec, *, timeout: float | None) -> ProcessRun:
        """Record ``spec``, let its running time pass, and end it the way its script says."""
        pid = self.first_pid + len(self.ran)
        self.ran.append(spec)
        script = self.script(spec)
        stdout = script.stdout + (spec.input if script.echo_input else b"")
        if too_large := _too_large(spec.max_output_bytes, stdout, script.stderr):
            return too_large
        endless = script.runs_until_signalled
        if timeout is not None and (endless or script.duration_s > timeout):
            self._elapse(timeout)
            if script.survives_kill:
                return ProcessGroupUnreaped(timeout, pid, f"process {pid} survived the kill")
            return ProcessTimedOut(timeout, script.stdout, script.stderr)
        if endless:
            message = f"{spec.argv[0]} never ends and the caller set no timeout"
            raise AssertionError(message)
        self._elapse(script.duration_s)
        return ProcessOutcome(script.returncode, stdout, script.stderr)

    def _elapse(self, seconds: float) -> None:
        if seconds > 0:
            self.pass_time(seconds)


class ProcessStartRefusedError(AssertionError):
    """A test that runs no process started one."""


_REFUSED: list[str] = []
"""Every refused start since the last :func:`refused_starts` call, as a description."""


def refused_starts() -> tuple[str, ...]:
    """The process starts that refusing Fakes turned away since the last call.

    A refusal raises, but product code that catches ``Exception`` swallows the raise, so
    the raise alone cannot fail a test. The test harness calls this after each test and
    fails the test if anything was refused. A test that checks a refusal itself calls this
    to take what it provoked.
    """
    refused = tuple(_REFUSED)
    _REFUSED.clear()
    return refused


class RefusingProcesses:
    """Starts nothing: any run fails the test, naming the command it tried to run.

    For a test whose code takes a :class:`~vs_sim.processes.Processes` it must never use.
    A test that does need a command to run scripts it with :class:`FakeProcesses` instead.
    """

    def run(self, spec: ProcessSpec, *, timeout: float | None) -> ProcessRun:
        """Fail: this test runs no process."""
        del timeout
        message = f"this test runs no process, but ran {' '.join(spec.argv)}"
        _REFUSED.append(message)
        raise ProcessStartRefusedError(message)


NO_PROCESSES = RefusingProcesses()
"""Shared :class:`RefusingProcesses`; it holds no state."""


@dataclass(frozen=True)
class PipedStart:
    """One request a :class:`FakePipedProcessLauncher` received."""

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    merge_stderr: bool = False


def _refuse_piped_start(request: PipedStart) -> PipedProcess:
    message = f"this test starts no piped process, but {request.argv[0]!r} was started"
    _REFUSED.append(message)
    raise ProcessStartRefusedError(message)


@dataclass
class FakePipedProcessLauncher:
    """Record synchronous piped process starts and create each process through a Fake factory.

    Without a factory, any start fails the test: the launcher stands in for one that the
    code under test is handed but must not use.
    """

    factory: Callable[[PipedStart], PipedProcess] = _refuse_piped_start
    started: list[PipedStart] = field(default_factory=list)

    def start(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        merge_stderr: bool = False,
    ) -> PipedProcess:
        """Record the normalized start request and return its scripted process."""
        request = PipedStart(
            tuple(argv), cwd, None if env is None else dict(env), merge_stderr=merge_stderr
        )
        self.started.append(request)
        return self.factory(request)


@dataclass(frozen=True)
class ForegroundScript:
    """What a scripted foreground child does."""

    returncode: int = 0
    exits_immediately: bool = False
    """End with ``returncode`` as soon as it is waited for; otherwise run until signalled or exited."""
    ignored_signals: frozenset[signal.Signals] = frozenset()
    """Signals it records but survives (a handler); any other signal ends it, as the default action does."""


class FakeForegroundChild:
    """A scripted child a test inspects and ends by hand."""

    def __init__(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str] | None,
        script: ForegroundScript,
        cwd: Path | None = None,
    ) -> None:
        """Record how it was started; it runs until the script or a signal ends it."""
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.received: list[signal.Signals] = []
        """Every signal delivered to it while it ran, in order."""
        self._script = script
        self._ended = asyncio.Event()
        self._returncode: int | None = script.returncode if script.exits_immediately else None
        if self._returncode is not None:
            self._ended.set()

    @property
    def running(self) -> bool:
        """Whether it has not ended."""
        return self._returncode is None

    def exit(self, returncode: int) -> None:
        """End it with ``returncode``; a no-op once it has ended."""
        if self._returncode is None:
            self._returncode = returncode
            self._ended.set()

    async def wait(self) -> int:
        """Wait for it to end."""
        await self._ended.wait()
        return self._returncode if self._returncode is not None else 0

    def send_signal(self, number: signal.Signals) -> None:
        """Record ``number``; end it with ``-number`` unless the script ignores it."""
        if self._returncode is not None:
            return
        self.received.append(number)
        if number not in self._script.ignored_signals:
            self.exit(-number)


@dataclass
class FakeForegroundLauncher:
    """Starts scripted foreground children and keeps them for the test to drive."""

    script: Callable[[tuple[str, ...]], ForegroundScript]
    children: list[FakeForegroundChild] = field(default_factory=list)
    _started: Changes = field(default_factory=Changes, init=False, repr=False)

    async def child(self, index: int = 0) -> FakeForegroundChild:
        """The ``index``-th child, once the code under test has started it."""
        await wait_for_state(lambda: len(self.children), lambda count: count > index, self._started)
        return self.children[index]

    async def start(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ForegroundChild:
        """Start the child the script describes for ``argv``."""
        child = FakeForegroundChild(tuple(argv), env, self.script(tuple(argv)), cwd)
        self.children.append(child)
        self._started.notify()
        return child


@dataclass
class ScriptedProbe:
    """A :class:`~vs_sim.probes.CommandProbe` whose answers come from a script.

    ``script`` maps an argument vector to what the command printed, or to ``None`` for a
    command that could not start or timed out.
    """

    script: Callable[[tuple[str, ...]], ProbeResult | None]
    calls: list[tuple[tuple[str, ...], float]] = field(default_factory=list)
    """Each probed argument vector with the timeout it was given."""

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProbeResult | None:
        """Record the call and answer as scripted."""
        command = tuple(argv)
        self.calls.append((command, timeout_seconds))
        return self.script(command)


@dataclass(frozen=True)
class StreamedRun:
    """What one scripted streaming command does, start to finish."""

    returncode: int = 0
    stdout: str = ""
    stderr_lines: tuple[str, ...] = ()
    """Each line it writes to standard error, without its line ending, in order."""
    blocks: bool = False
    """It never ends by itself, so it outlives any timeout and is killed."""


@dataclass
class ScriptedStreamingCommand:
    """A :class:`~vs_sim.streaming.StreamingCommand` whose commands come from a script.

    ``script`` maps an argument vector and working directory to what that command does,
    or to ``None`` for a program that does not exist. Like the real command, each stderr
    line reaches the callback on the calling thread before :meth:`run` returns or raises.
    """

    script: Callable[[tuple[str, ...], Path], StreamedRun | None]
    calls: list[tuple[tuple[str, ...], Path, float]] = field(default_factory=list)
    """Each argument vector run, with its working directory and timeout."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        on_stderr_line: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        """Record the call and play the scripted command."""
        command = tuple(argv)
        self.calls.append((command, cwd, timeout_seconds))
        scripted = self.script(command, cwd)
        if scripted is None:
            raise FileNotFoundError(2, "No such file or directory", command[0])
        for line in scripted.stderr_lines:
            on_stderr_line(line)
        stderr = "".join(f"{line}\n" for line in scripted.stderr_lines)
        if scripted.blocks:
            raise subprocess.TimeoutExpired(command, timeout_seconds, scripted.stdout, stderr)
        return subprocess.CompletedProcess(command, scripted.returncode, scripted.stdout, stderr)


class FakeLoopbackHttp:
    """Loopback HTTP whose servers are ports a test serves by hand."""

    def __init__(self) -> None:
        """Start with nothing listening."""
        self._served: dict[int, Callable[[str], HttpReply]] = {}
        self.requests: list[tuple[int, str, dict[str, str]]] = []

    def serve(self, port: int, answer: Callable[[str], HttpReply]) -> None:
        """Answer requests to ``port`` with ``answer(target)``."""
        self._served[port] = answer

    def stop(self, port: int) -> None:
        """Stop answering on ``port``, as if its server went away."""
        self._served.pop(port, None)

    def get(
        self,
        port: int,
        target: str,
        *,
        timeout: float,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpReply | None:
        """Record the request and its headers; the served answer cut to ``max_bytes``, or ``None``."""
        del timeout
        self.requests.append((port, target, dict(headers or {})))
        answer = self._served.get(port)
        if answer is None:
            return None
        reply = answer(target)
        return HttpReply(reply.status, reply.body[:max_bytes])
