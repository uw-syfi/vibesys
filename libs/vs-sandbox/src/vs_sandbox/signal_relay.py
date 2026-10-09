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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from types import FrameType


def _ignore(_signum: int, _frame: FrameType | None) -> None:
    """Replace the default action; the relay thread does the work."""


@contextmanager
def relay_signals(
    numbers: Iterable[signal.Signals], on_signal: Callable[[int], None]
) -> Iterator[None]:
    """Call ``on_signal(number)`` on a relay thread for each of *numbers* received.

    Must be entered on the main thread. *on_signal* runs off the main thread, so
    it must be thread-safe. Handlers and the wakeup fd are restored on exit.
    """
    watched = {int(number) for number in numbers}
    reader, writer = socket.socketpair()
    writer.settimeout(0)

    def pump() -> None:
        while data := reader.recv(64):
            for byte in data:
                if byte in watched:
                    on_signal(byte)

    relay = threading.Thread(target=pump, name="signal-relay", daemon=True)
    previous = {number: signal.signal(number, _ignore) for number in watched}
    previous_fd = signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
    relay.start()
    try:
        yield
    finally:
        signal.set_wakeup_fd(previous_fd)
        for number, handler in previous.items():
            signal.signal(number, handler)
        writer.close()
        relay.join()
        reader.close()
