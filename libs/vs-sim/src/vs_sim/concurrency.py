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
import threading
import time
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


class WorkerStartRefusedError(RuntimeError):
    """The operating system refused to start a requested worker thread."""

    def __init__(self, name: str) -> None:
        """Name the worker whose start was refused."""
        super().__init__(f"worker start refused: {name}")


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


class OsThreads:
    """Operating-system threads and the standard library's primitives."""

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
