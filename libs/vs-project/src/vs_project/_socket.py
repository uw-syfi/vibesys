"""Length validation for the Unix domain sockets VibeSys listens on.

A ``AF_UNIX`` address is a fixed-size ``sockaddr_un.sun_path`` character array,
so the kernel refuses to bind a path that does not fit: 108 bytes on Linux and
104 on macOS, both counting the terminating NUL. Every listening path VibeSys
builds is derived from a user-chosen directory (a run's log directory for the
SkyPilot bridge, a session directory for an application server), so a deep
enough workspace pushes the socket past the limit.

The raw failure is an ``OSError`` reading ``AF_UNIX path too long`` that names
neither the path nor the limit, which is a poor diagnostic for something the
user controls. Bind sites call :func:`validate_socket_path` first so the error
says which path is too long, by how much, and on which platform.
"""

from __future__ import annotations

import errno
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

MAX_SOCKET_PATH_BYTES: int = 103 if sys.platform == "darwin" else 107
"""Longest bindable ``sun_path``, in bytes, excluding the trailing NUL."""


class SocketPathTooLongError(OSError):
    """A listening path does not fit in ``sockaddr_un.sun_path``.

    Subclasses ``OSError`` because it replaces the kernel's own ``OSError``:
    callers that already treat a failed bind as an ``OSError`` keep working.
    """

    def __init__(self, path: Path, limit: int):  # noqa: ANN204  # tracked: #288
        encoded = len(str(path).encode())
        super().__init__(
            errno.ENAMETOOLONG,
            f"Unix socket path is {encoded} bytes, over this platform's "
            f"{limit}-byte limit: {path}. Choose a shorter directory.",
        )
        self.path = path
        self.limit = limit


def validate_socket_path(path: Path) -> Path:
    """Return ``path`` if it is bindable, else raise :class:`SocketPathTooLongError`."""
    if len(str(path).encode()) > MAX_SOCKET_PATH_BYTES:
        raise SocketPathTooLongError(path, MAX_SOCKET_PATH_BYTES)
    return path
