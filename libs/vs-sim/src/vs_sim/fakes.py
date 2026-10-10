"""In-memory Fakes of the vs_sim interfaces, for tests and simulations."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vs_sim.gate import arrival
from vs_sim.processes import ProcessOutcome, ProcessSpec, RunningProcess

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.clock import Sleeper


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
