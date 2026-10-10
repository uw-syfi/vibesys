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

import threading
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
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

    def sleep(self, seconds: float) -> None:
        """Block the calling thread for about ``seconds``; other threads run meanwhile."""
        ...

    def now(self) -> float:
        """Monotonic seconds; only differences are meaningful."""
        ...


class OsThreads:
    """Operating-system threads and the standard library's primitives."""

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        """Start ``target`` on a new ``threading.Thread``."""
        thread = threading.Thread(target=target, name=name, daemon=daemon)
        thread.start()
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

    def sleep(self, seconds: float) -> None:
        """``time.sleep``."""
        time.sleep(max(seconds, 0.0))

    def now(self) -> float:
        """``time.monotonic``."""
        return time.monotonic()
