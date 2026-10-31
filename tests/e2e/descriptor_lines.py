"""The lines a child writes, read through its descriptor so that ``select`` sees every byte.

A buffered ``readline`` takes whatever the pipe holds, so two lines the child wrote close
together arrive in one read: the first ``readline`` keeps the second in the stream's own buffer,
and a ``select`` on the descriptor then waits for bytes that are not coming, until its bound
runs out. A test that waits for a child's reports this way fails by timing, with the child alive
and the report already read. ``DescriptorLines`` owns its buffer and reads the descriptor itself,
so a line is waited for only while no whole line is held, and ``bound`` only guards against a
child that never reports. Use it, and no other mix of ``select`` and a buffered read, for
a child's lines.
"""

from __future__ import annotations

import os
import select


class DescriptorLines:
    """Whole lines read from one descriptor, kept in this reader's own buffer."""

    def __init__(self, descriptor: int, *, bound: float) -> None:
        """Read *descriptor*, waiting at most *bound* seconds for the bytes of a line.

        *bound* is a hang guard: a line that is coming is not waited for by the clock.
        """
        self._descriptor = descriptor
        self._bound = bound
        self._pending = b""

    def next_line(self, *, unless_readable: int | None = None) -> str | None:
        """The next line, without its newline.

        With *unless_readable*, a descriptor that says no more lines will come (the child's
        own process, say, ending), ``None`` once it is readable and no whole line is held.
        Raises ``AssertionError`` when the child ends, or stays silent for the bound, before
        a line is whole.
        """
        watched = (
            [self._descriptor] if unless_readable is None else [self._descriptor, unless_readable]
        )
        while b"\n" not in self._pending:
            ready, _, _ = select.select(watched, [], [], self._bound)
            assert ready, f"the process reported nothing within {self._bound:g} s"
            if self._descriptor not in ready:
                return None
            chunk = os.read(self._descriptor, 4096)
            assert chunk, "the process ended before it reported"
            self._pending += chunk
        line, _, self._pending = self._pending.partition(b"\n")
        return line.decode()
