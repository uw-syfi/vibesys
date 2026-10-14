"""Deliver termination signals to a callback regardless of which thread the kernel picked.

The kernel may hand a process-directed signal to any thread, but CPython runs
Python-level handlers on the main thread only. A main thread blocked in
``Thread.join()`` or ``read()`` is not interrupted when another thread took the
signal, so a handler-based stop request can be lost. The C-level handler
writes the signal number to the wakeup fd on whichever thread received it, so
a relay thread reading that fd sees every signal.
"""

from __future__ import annotations

import signal
import socket
import threading
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from contextlib import AbstractContextManager
    from types import FrameType

    from vs_sim.concurrency import Threads


def _ignore(_signum: int, _frame: FrameType | None) -> None:
    """Replace the default action; the relay thread does the work."""


class SignalRelay(Protocol):
    """Where a process learns, on any thread, that a termination signal arrived."""

    def relay(
        self, numbers: Iterable[int], on_signal: Callable[[int], None]
    ) -> AbstractContextManager[None]:
        """Call ``on_signal(number)`` off the main thread for each of *numbers* received while active.

        Must be entered on the main thread; *on_signal* must be thread-safe. Leaving the
        block restores what the process had before and ends the relay.
        """
        ...


class WakeupFdSignalRelay:
    """:class:`SignalRelay` over the interpreter's wakeup file descriptor."""

    def __init__(self, threads: Threads) -> None:
        """Run the relay thread on *threads*."""
        self._threads = threads

    @contextmanager
    def relay(self, numbers: Iterable[int], on_signal: Callable[[int], None]) -> Iterator[None]:
        """Relay *numbers* to *on_signal* on a thread; handlers and the wakeup fd are restored on exit."""
        watched = {int(number) for number in numbers}
        reader, writer = socket.socketpair()
        writer.settimeout(0)

        def pump() -> None:
            while data := reader.recv(64):
                for byte in data:
                    if byte in watched:
                        on_signal(byte)

        previous = {number: signal.signal(number, _ignore) for number in watched}
        previous_fd = signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
        worker = self._threads.spawn(pump, name="signal-relay", daemon=True)
        try:
            yield
        finally:
            signal.set_wakeup_fd(previous_fd)
            for number, handler in previous.items():
                signal.signal(number, handler)
            writer.close()
            worker.join()
            reader.close()


class MainThreadWake(Protocol):
    """How another thread makes the main thread run a callback, ending the wait it is blocked in."""

    def armed(self, on_wake: Callable[[], None]) -> AbstractContextManager[Callable[[], None]]:
        """Arm the wake for the block; it yields the call that wakes the main thread.

        Must be entered on the main thread. The yielded call is safe from any thread and
        makes *on_wake* run on the main thread; *on_wake* may raise there (for example
        ``KeyboardInterrupt``) to end what the main thread was doing. A wake may land
        before the main thread blocks and so end nothing; callers resend until answered.
        Leaving the block restores what the process had before.
        """
        ...


class SignalMainThreadWake:
    """:class:`MainThreadWake` through a private signal sent to the main thread alone."""

    def __init__(self, number: signal.Signals = signal.SIGUSR1) -> None:
        """Wake with *number*, which nothing else in the process may use while armed."""
        self._number = number

    @contextmanager
    def armed(self, on_wake: Callable[[], None]) -> Iterator[Callable[[], None]]:
        """Install the wake signal's handler; it is restored on exit."""

        def handler(_signum: int, _frame: FrameType | None) -> None:
            on_wake()

        previous = signal.signal(self._number, handler)
        try:
            yield partial(signal.pthread_kill, threading.get_ident(), self._number)
        finally:
            signal.signal(self._number, previous)
