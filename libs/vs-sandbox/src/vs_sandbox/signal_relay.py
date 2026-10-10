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
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

from vs_sim.api import OsThreads, Threads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from contextlib import AbstractContextManager
    from types import FrameType


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

    def __init__(self, threads: Threads | None = None) -> None:
        """Run the relay thread on *threads* (operating-system threads by default)."""
        self._threads: Threads = threads or OsThreads()

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


def relay_signals(
    numbers: Iterable[int], on_signal: Callable[[int], None]
) -> AbstractContextManager[None]:
    """Relay *numbers* to *on_signal* through the process's real signals; see :class:`SignalRelay`."""
    return WakeupFdSignalRelay().relay(numbers, on_signal)
