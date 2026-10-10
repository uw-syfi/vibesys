"""An in-memory :class:`~vs_sandbox.signal_relay.SignalRelay` whose signals a test delivers by hand."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator


class FakeSignalRelay:
    """Relays nothing on its own: ``deliver`` stands in for the kernel handing over a signal."""

    def __init__(self) -> None:
        """Start with no active relay."""
        self._active: tuple[frozenset[int], Callable[[int], None]] | None = None

    @property
    def active(self) -> bool:
        """Whether a ``relay`` block is open."""
        return self._active is not None

    @contextmanager
    def relay(self, numbers: Iterable[int], on_signal: Callable[[int], None]) -> Iterator[None]:
        """Relay *numbers* to *on_signal* until the block ends."""
        if self._active is not None:
            message = "a FakeSignalRelay serves one relay block at a time"
            raise RuntimeError(message)
        self._active = (frozenset(int(number) for number in numbers), on_signal)
        try:
            yield
        finally:
            self._active = None

    def deliver(self, number: int) -> bool:
        """Call the active ``on_signal`` for *number*; whether it was watched and delivered."""
        if self._active is None or int(number) not in self._active[0]:
            return False
        self._active[1](int(number))
        return True
