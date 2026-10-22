"""Incoming and outgoing process signals behind deterministic interfaces."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


_FLOCK_FIELDS = 9

Signals = signal.Signals
"""Signal numbers (``Signals.SIGTERM``), so callers name them without importing ``signal``."""


class SignalSource(Protocol):
    """Where a process learns that a signal arrived."""

    def add_handler(self, number: signal.Signals, handler: Callable[[], None]) -> None:
        """Call ``handler`` when ``number`` arrives, replacing an earlier handler for it."""
        ...

    def remove_handler(self, number: signal.Signals) -> bool:
        """Stop handling ``number``; whether a handler was installed."""
        ...


class LoopSignalSource:
    """The process's own signals, delivered on the running event loop (Unix).

    Only the main thread receives a process's signals, so on any other thread this
    source installs nothing and ``remove_handler`` reports false. Removing a handler
    restores the disposition the signal had before this source first took it, so a
    scope that handles signals leaves the process as it found it.
    """

    def __init__(self) -> None:
        """Take no signal until a handler is added."""
        self._previous: dict[
            signal.Signals, signal.Handlers | Callable[..., object] | int | None
        ] = {}

    def add_handler(self, number: signal.Signals, handler: Callable[[], None]) -> None:
        """Install ``handler`` on the running loop, on the main thread only."""
        if threading.current_thread() is not threading.main_thread():
            return
        if number not in self._previous:
            self._previous[number] = signal.getsignal(number)
        asyncio.get_running_loop().add_signal_handler(number, handler)

    def remove_handler(self, number: signal.Signals) -> bool:
        """Remove the loop's handler for ``number`` and restore its earlier disposition."""
        if number not in self._previous:
            return False
        removed = asyncio.get_running_loop().remove_signal_handler(number)
        previous = self._previous.pop(number)
        if previous is not None:
            signal.signal(number, previous)
        return removed


class ProcessSignaller(Protocol):
    """Terminate an existing process only after validating a stable identity for it."""

    def exists(self, pid: int) -> bool:
        """Whether a process with id ``pid`` exists now, whoever owns it.

        Only a definite absence is false: a process another user owns exists. The answer
        names no stable identity, so an id the kernel reused also exists.
        """
        ...

    def terminate_if_current(self, pid: int, current: Callable[[], bool]) -> bool:
        """Send SIGTERM to stable ``pid`` iff ``current`` remains true; whether it was sent.

        The implementation acquires its stable process reference before evaluating
        ``current``. It raises ``ProcessLookupError`` when ``pid`` no longer exists and
        ``NotImplementedError`` when the host cannot provide a stable reference.
        """
        ...


class WriterProcessSignaller(ProcessSignaller, Protocol):
    """Signal a writer only with proof that it owns its exclusive lifetime flock."""

    def owns_lock(self, pid: int, path: Path) -> bool:
        """Whether ``pid`` holds an exclusive flock on this exact file inode.

        A held lock elsewhere or a matching numeric PID is insufficient. Raises
        ``NotImplementedError`` without kernel ownership inspection, and ``OSError``
        if ownership cannot be observed. Call after pinning the process handle.
        """
        ...


class PidfdProcessSignaller:
    """Linux pidfd-backed signalling that cannot target a reused process ID.

    It uses the interpreter's pidfd wrappers when it has them and calls the
    Linux system calls directly otherwise; ``direct_syscalls`` forces the latter.
    """

    def __init__(self, *, direct_syscalls: bool = False) -> None:
        """Choose the interpreter wrappers (default) or force direct system calls."""
        self._direct_syscalls = direct_syscalls

    def exists(self, pid: int) -> bool:
        """Probe ``pid`` with signal 0, which checks for the process and delivers nothing."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def owns_lock(self, pid: int, path: Path) -> bool:
        """Bind the writer's lifetime flock to the process visible through procfs."""
        if not sys.platform.startswith("linux"):
            raise NotImplementedError("process lock ownership requires Linux procfs")
        expected = path.stat()
        identity = (
            f"{os.major(expected.st_dev):02x}:{os.minor(expected.st_dev):02x}:{expected.st_ino}"
        )
        for info in (Path("/proc") / str(pid) / "fdinfo").iterdir():
            try:
                lines = info.read_text().splitlines()
            except FileNotFoundError:
                continue  # A descriptor closed during inspection supplies no proof.
            for line in lines:
                parts = line.split()
                if (
                    len(parts) == _FLOCK_FIELDS
                    and parts[0] == "lock:"
                    and parts[2:6] == ["FLOCK", "ADVISORY", "WRITE", str(pid)]
                    and parts[6:] == [identity, "0", "EOF"]
                ):
                    return True
        return False

    def terminate_if_current(self, pid: int, current: Callable[[], bool]) -> bool:
        """Open ``pid``, revalidate caller identity, and send SIGTERM through its pidfd."""
        pidfd_open, pidfd_send_signal = _pidfd_calls(direct=self._direct_syscalls)
        descriptor = pidfd_open(pid)
        try:
            if not current():
                return False
            pidfd_send_signal(descriptor, signal.SIGTERM)
            return True
        finally:
            os.close(descriptor)


# Linux assigns these two system calls one number on every architecture (they
# arrived after the syscall tables were unified), so they can be called directly
# when the interpreter was built without the wrappers, as the standalone builds
# uv installs are.
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434


def _pidfd_calls(*, direct: bool) -> tuple[Callable[[int], int], Callable[[int, int], None]]:
    """The interpreter's pidfd wrappers, else (or when *direct*) Linux system calls."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not direct and pidfd_open is not None and pidfd_send_signal is not None:
        return pidfd_open, pidfd_send_signal
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("safe process signalling requires Linux pidfds")
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(result: int) -> int:
        if result < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        return result

    def open_pidfd(pid: int) -> int:
        return checked(libc.syscall(_SYS_PIDFD_OPEN, ctypes.c_int(pid), ctypes.c_uint(0)))

    def send_signal(descriptor: int, number: int) -> None:
        checked(
            libc.syscall(
                _SYS_PIDFD_SEND_SIGNAL,
                ctypes.c_int(descriptor),
                ctypes.c_int(number),
                None,
                ctypes.c_uint(0),
            )
        )

    return open_pidfd, send_signal
