"""Threads and the primitives that block them, behind one interface a simulator can drive.

Product code that needs a background thread, a lock, a condition, an event or a
synchronous sleep asks a :class:`Threads` for it instead of importing ``threading`` and
``time``. Production wiring passes :class:`OsThreads`, whose objects are the standard
library's own, so nothing changes at run time. A test passes the cooperative simulator
from :mod:`vs_sim.api.testing`, which runs every thread one at a time in a seeded order
on the virtual timeline.

The primitives here are the subset of ``threading`` that the product uses; their
behavior is that of the standard library (the contract suite checks both
implementations against the same cases).
"""

from __future__ import annotations

import asyncio
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import TracebackType


class Lock(Protocol):
    """A mutual-exclusion lock; ``Threads.rlock`` returns one the owner may re-acquire."""

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:  # noqa: FBT001, FBT002  # LW-163901 [FBT001, FBT002]; the signature of ``threading.Lock.acquire``, which this protocol mirrors.
        """Take the lock; ``False`` when it is not available (non-blocking) or ``timeout`` ran out."""
        ...

    def release(self) -> None:
        """Give the lock up; ``RuntimeError`` when the caller does not hold it."""
        ...

    def __enter__(self) -> object:
        """Acquire."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        """Release."""
        ...


class Condition(Lock, Protocol):
    """A condition variable over a lock, with ``threading.Condition`` semantics."""

    def wait(self, timeout: float | None = None) -> bool:
        """Release the lock until notified or ``timeout`` runs out; ``False`` on timeout.

        The lock is held again on return. ``RuntimeError`` when the caller does not hold it.
        """
        ...

    def wait_for(self, predicate: Callable[[], bool], timeout: float | None = None) -> bool:
        """Wait until ``predicate()`` is true; its last value (``False`` after a timeout)."""
        ...

    def notify(self, n: int = 1) -> None:
        """Wake up to ``n`` waiters. ``RuntimeError`` when the caller does not hold the lock."""
        ...

    def notify_all(self) -> None:
        """Wake every waiter. ``RuntimeError`` when the caller does not hold the lock."""
        ...


class Event(Protocol):
    """A flag threads can wait for."""

    def is_set(self) -> bool:
        """Whether the flag is set."""
        ...

    def set(self) -> None:
        """Set the flag and wake every waiter."""
        ...

    def clear(self) -> None:
        """Clear the flag."""
        ...

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the flag is set or ``timeout`` runs out; the flag's value on return."""
        ...


class Worker(Protocol):
    """A started thread."""

    @property
    def name(self) -> str:
        """The name it was spawned with."""
        ...

    def is_alive(self) -> bool:
        """Whether the target is still running."""
        ...

    def join(self, timeout: float | None = None) -> None:
        """Wait for the thread to end, or for ``timeout`` to run out (then check ``is_alive``)."""
        ...


class WorkerReservation(Protocol):
    """A worker owned before its target can start.

    This two-step form is for transactions that must retain cleanup ownership even when
    starting the underlying native thread is interrupted after taking effect. ``settle``
    is used only on an aborted transaction; it is idempotent, consumes an unstarted
    reservation, and returns once any possibly-started target has ended.
    """

    @property
    def name(self) -> str:
        """The name the worker will have."""
        ...

    def start(self) -> None:
        """Attempt the start once; the reservation retains ownership if this raises."""
        ...

    def settle(self) -> None:
        """Wait for a possibly-started target to end after its owner has made that safe.

        An asynchronous ``BaseException`` may interrupt the wait; the owner can call
        ``settle`` again to continue. A completed settlement is terminal: a later
        ``start`` raises :class:`WorkerReservationStateError`. Implementations do not
        otherwise fail settlement.
        """
        ...


class WorkerStartRefusedError(RuntimeError):
    """The operating system refused to start a requested worker thread."""

    def __init__(self, name: str) -> None:
        """Name the worker whose start was refused."""
        super().__init__(f"worker start refused: {name}")


class WorkerReservationStateError(RuntimeError):
    """A worker reservation no longer accepts a start attempt."""

    def __init__(self, name: str) -> None:
        """Name the worker whose reservation was attempted or settled."""
        super().__init__(f"worker reservation cannot be started: {name}")


class ThreadLocal[T](Protocol):
    """A value each thread sees its own copy of, made on that thread's first ``get``."""

    def get(self) -> T:
        """The calling thread's value."""
        ...


class Threads(Protocol):
    """Everything product code uses to run work on other threads and to wait on them.

    ``now`` is a monotonic reading on the timeline that ``sleep`` and every timeout use,
    so deadlines computed from it agree with the waits.
    """

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        """Start ``target`` on a new thread and return its handle."""
        ...

    def lock(self) -> Lock:
        """A new non-reentrant lock."""
        ...

    def rlock(self) -> Lock:
        """A new reentrant lock."""
        ...

    def condition(self, lock: Lock | None = None) -> Condition:
        """A new condition over ``lock`` (a new reentrant lock when omitted)."""
        ...

    def event(self) -> Event:
        """A new, cleared event."""
        ...

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        """A per-thread value; each thread's starts as ``initial()``."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block the calling thread for about ``seconds``; other threads run meanwhile."""
        ...

    def now(self) -> float:
        """Monotonic seconds; only differences are meaningful."""
        ...

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        """Run ``main`` to completion on a new event loop on the calling thread."""
        ...


class ReservableThreads(Threads, Protocol):
    """Threads that can be owned before an interruption-prone start is attempted."""

    def reserve(
        self, target: Callable[[], object], *, name: str, daemon: bool = True
    ) -> WorkerReservation:
        """Own a not-yet-started worker whose interrupted start can still be settled."""
        ...


class OsThreads:
    """Operating-system threads and the standard library's primitives."""

    def __init__(self, *, _starter: _ProcessWorkerStarter | None = None) -> None:
        """Use the process starter, or an isolated one supplied by the public test harness."""
        self._starter = _STARTER_OWNER if _starter is None else _starter

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        """Start ``target`` on a new ``threading.Thread``."""
        thread = threading.Thread(target=target, name=name, daemon=daemon)
        try:
            thread.start()
        except RuntimeError as error:
            # This is a fresh Thread, so Thread.start()'s usual programming
            # error (starting one object twice) is impossible. The remaining
            # RuntimeError is the runtime refusing a new native thread.
            raise WorkerStartRefusedError(name) from error
        return thread

    def reserve(
        self, target: Callable[[], object], *, name: str, daemon: bool = True
    ) -> WorkerReservation:
        """Construct a native worker without starting it."""
        try:
            self._starter.get()
        except RuntimeError as error:
            # Dispatcher bootstrap is part of preparing this reservation. Preserve
            # ownership even when the runtime refuses that internal native worker:
            # start reports the typed refusal and settle remains idempotent.
            return _RefusedOsWorkerReservation(name, error)
        return _OsWorkerReservation(self._starter, target, name=name, daemon=daemon)

    def lock(self) -> Lock:
        """A ``threading.Lock``."""
        return threading.Lock()

    def rlock(self) -> Lock:
        """A ``threading.RLock``."""
        return threading.RLock()

    def condition(self, lock: Lock | None = None) -> Condition:
        """A ``threading.Condition``."""
        # ``threading.Condition`` accepts any object with the lock methods.
        return threading.Condition(lock)  # ty: ignore[invalid-argument-type]

    def event(self) -> Event:
        """A ``threading.Event``."""
        return threading.Event()

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        """A ``threading.local`` holding each thread's value."""
        return OsThreadLocal(initial)

    def sleep(self, seconds: float) -> None:
        """``time.sleep``."""
        time.sleep(max(seconds, 0.0))

    def now(self) -> float:
        """``time.monotonic``."""
        return time.monotonic()

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        """``asyncio.run``."""
        return asyncio.run(main)


@dataclass(frozen=True)
class _Started:
    """Positive evidence that the native target thread was started."""


@dataclass(frozen=True)
class _StartRefused:
    """Evidence that the native target thread was not started."""

    cause: BaseException


type _StartOutcome = _Started | _StartRefused


class _OsStartRequest:
    """One idempotently dispatchable native start and its closed outcome."""

    def __init__(
        self,
        target: Callable[[], object],
        *,
        name: str,
        daemon: bool,
        target_mask: frozenset[signal.Signals | int],
    ) -> None:
        self._target = target
        self._target_mask = target_mask
        self.target_finished = threading.Event()
        self.outcome_ready = threading.Event()
        self.outcome: _StartOutcome | None = None
        self.thread = threading.Thread(target=self._run, name=name, daemon=daemon)

    def dispatch(self) -> None:
        """Publish exactly one native-start outcome on the starter worker."""
        if self.outcome_ready.is_set():
            return
        try:
            self.thread.start()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-883273 [BLE001]; this dispatcher-owned boundary must turn every native-start failure into a closed refusal outcome or an interrupted owner could wait forever; restricting the exception set would make settlement unsound for VM-level failures.
            self.outcome = _StartRefused(error)
        else:
            self.outcome = _Started()
        finally:
            self.outcome_ready.set()

    def _run(self) -> None:
        """Restore the reserving thread's mask before user code and publish its end."""
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, self._target_mask)
            self._target()
        finally:
            self.target_finished.set()


class _OsWorkerStarter:
    """Serialize target starts outside their interruption-prone owners.

    This daemon owns no external resource and intentionally lives until its process exits.
    It is launched at the first reservation boundary, before that reservation is exposed.
    Target-generated signals can therefore interrupt only an owner's outcome wait, never
    either native ``Thread.start`` call.
    """

    def __init__(self) -> None:
        self.process_id = os.getpid()
        self._requests: queue.SimpleQueue[_OsStartRequest] = queue.SimpleQueue()
        self._worker = threading.Thread(
            target=self._run,
            name="vs-sim-worker-starter",
            daemon=True,
        )

    def launch(self) -> None:
        """Start the dispatcher after its process owner has published it."""
        self._worker.start()

    def submit(self, request: _OsStartRequest) -> None:
        """Publish or re-publish a request; dispatch itself is idempotent."""
        self._requests.put(request)

    def _run(self) -> None:
        while True:
            self._requests.get().dispatch()


type _StarterLauncher = Callable[[_OsWorkerStarter], None]


def _launch_starter(starter: _OsWorkerStarter) -> None:
    starter.launch()


class _ProcessWorkerStarter:
    """Own one process-lifetime starter and discard all native state after ``fork``."""

    def __init__(self, launch: _StarterLauncher = _launch_starter) -> None:
        self._lock = threading.Lock()
        self._starter: _OsWorkerStarter | None = None
        self._launch = launch

    def get(self) -> _OsWorkerStarter:
        """Return this process's pre-started dispatcher."""
        process_id = os.getpid()
        with self._lock:
            if self._starter is None or self._starter.process_id != process_id:
                # No target request exists yet. Mask every process signal across
                # bootstrap, and publish ownership before launch. A pending signal
                # delivered while the old mask is restored can then interrupt this
                # call, but retry still reaches the one live dispatcher.
                blockable = signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}
                old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blockable)
                try:
                    starter = _OsWorkerStarter()
                    self._starter = starter
                    try:
                        self._launch(starter)
                    except RuntimeError:
                        # Signals are still masked, so a fresh Thread's documented
                        # RuntimeError positively means no dispatcher was launched.
                        self._starter = None
                        raise
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            return self._starter

    def submit(self, request: _OsStartRequest) -> None:
        """Queue through the current process owner, never an inherited worker."""
        self.get().submit(request)

    def after_fork(self) -> None:
        """Forget the vanished worker, its queue, and possibly inherited lock state."""
        self._lock = threading.Lock()
        self._starter = None


_STARTER_OWNER = _ProcessWorkerStarter()
os.register_at_fork(after_in_child=_STARTER_OWNER.after_fork)


class _RefusedOsWorkerReservation:
    """A closed reservation for a refused process-dispatcher bootstrap."""

    def __init__(self, name: str, cause: RuntimeError) -> None:
        self._name = name
        self._cause = cause
        self._attempted = False
        self._settled = False

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        if self._attempted or self._settled:
            raise WorkerReservationStateError(self.name)
        self._attempted = True
        raise WorkerStartRefusedError(self.name) from self._cause

    def settle(self) -> None:
        self._settled = True


class _OsWorkerReservation:
    """A native target whose start outcome is owned outside the calling thread."""

    def __init__(
        self,
        starter: _ProcessWorkerStarter,
        target: Callable[[], object],
        *,
        name: str,
        daemon: bool,
    ) -> None:
        self._starter = starter
        target_mask = frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
        self._request = _OsStartRequest(
            target,
            name=name,
            daemon=daemon,
            target_mask=target_mask,
        )
        self._state = _ReservationState.PREPARED

    @property
    def name(self) -> str:
        return self._request.thread.name

    def start(self) -> None:
        if self._state is not _ReservationState.PREPARED:
            raise WorkerReservationStateError(self.name)
        # Publish intent before any interruption-prone call. If submit or the wait is
        # interrupted, settle re-submits the same idempotent request and obtains the
        # process dispatcher's authoritative outcome.
        self._state = _ReservationState.REQUESTED
        self._starter.submit(self._request)
        self._request.outcome_ready.wait()
        outcome = self._request.outcome
        match outcome:
            case _StartRefused(cause):
                self._state = _ReservationState.REFUSED
                raise WorkerStartRefusedError(self.name) from cause
            case _Started():
                self._state = _ReservationState.STARTED
            case None:
                message = f"starter published no outcome for {self.name}"
                raise RuntimeError(message)

    def settle(self) -> None:
        if self._state is _ReservationState.SETTLED:
            return
        if self._state is _ReservationState.PREPARED:
            self._state = _ReservationState.SETTLED
            return
        # Submit may have been interrupted on either side of its queue publication.
        # Re-submission is harmless because dispatch publishes exactly one outcome.
        if self._state is _ReservationState.REQUESTED:
            self._starter.submit(self._request)
        self._request.outcome_ready.wait()
        outcome = self._request.outcome
        match outcome:
            case _Started():
                # Thread.join() is not completion evidence after an asynchronous exception:
                # CPython can release its internal state lock and mark a live thread stopped.
                # Only the target-owned event proves user code has ended.
                self._request.target_finished.wait()
            case _StartRefused():
                pass
            case None:
                message = f"starter published no outcome for {self.name}"
                raise RuntimeError(message)
        self._state = _ReservationState.SETTLED


class _ReservationState(StrEnum):
    """The one-way lifecycle of a native worker reservation."""

    PREPARED = "prepared"
    REQUESTED = "requested"
    STARTED = "started"
    REFUSED = "refused"
    SETTLED = "settled"


class OsThreadLocal[T](threading.local):
    """A ``threading.local`` value: every OS thread sees its own."""

    def __init__(self, initial: Callable[[], T]) -> None:
        """Make each thread's value with ``initial`` the first time that thread asks."""
        self._initial = initial

    def get(self) -> T:
        """The calling thread's value, made by ``initial`` on its first call."""
        try:
            return self._value
        except AttributeError:
            self._value: T = self._initial()
            return self._value
