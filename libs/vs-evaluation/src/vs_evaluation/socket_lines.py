"""Line framing shared by the host services that agent tool servers dial over a unix socket.

The queue status wire and the tool relay both send one JSON request line and read one
JSON reply line. Reading that line within a deadline, and refusing one that never ends,
is the same rule for both, so it lives here once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sim.api import Clock, Connection


class LineTooLongError(OSError):
    """A peer sent more than the frame allows without ending its line."""


def read_line(connection: Connection, clock: Clock, deadline: float, limit: int) -> bytes | None:
    """One line, or ``None`` if the peer closed first; ``TimeoutError`` at ``deadline``.

    Raises:
        TimeoutError: no complete line arrived before ``deadline`` (seconds on ``clock``).
        LineTooLongError: more than ``limit`` bytes arrived without a newline.
    """
    data = b""
    while b"\n" not in data:
        remaining = deadline - clock.now()
        if remaining <= 0:
            message = "no complete line before the deadline"
            raise TimeoutError(message)
        chunk = connection.recv(65536, remaining)
        if not chunk:
            return None
        data += chunk
        if len(data) > limit:
            message = f"line longer than {limit} bytes"
            raise LineTooLongError(message)
    return data.partition(b"\n")[0]


__all__ = ["LineTooLongError", "read_line"]
