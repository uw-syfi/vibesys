"""Find the processes that carry a mark in their environment, and end them by identity.

A mark is an environment variable a process started with and its descendants inherit, so the
processes of work a dead owner started are found by the mark alone. A process is named by its
pid and the kernel's start time of it: a pid the kernel reused for another process has another
start time and is never ended.
"""

from __future__ import annotations

import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_sim.signals import signal_if_current

if TYPE_CHECKING:
    from collections.abc import Callable

_PROC = Path("/proc")
_STAT_FIELDS_AFTER_COMM = 20
"""Fields of ``/proc/<pid>/stat`` between the command's closing parenthesis and the start time."""


class ProcessTableUnavailableError(RuntimeError):
    """The host cannot list the environments of its processes."""


@dataclass(frozen=True, slots=True)
class MarkedProcess:
    """A live process that carries a mark, named so that a reused pid is a different process."""

    pid: int
    started: int
    """The kernel's start time of the process (clock ticks since boot on Linux)."""
    value: str
    """What the mark was set to."""


class ProcessTable(Protocol):
    """The host's processes, by the marks in their initial environments."""

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """Every live process whose environment sets *name* to a value *matches* accepts.

        A zombie holds no environment and is not listed. Raises
        :class:`ProcessTableUnavailableError` when the host cannot list them.
        """
        ...

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """End *process* with SIGKILL iff the pid is still that process and still carries the mark.

        Returns whether the signal was sent: a process already gone, or a pid another process
        now holds, is left alone and reported ``False``.
        """
        ...


class OsProcessTable:
    """The Linux process table, read from ``/proc``."""

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """List the marked processes the current user may read."""
        _require_procfs()
        found: list[MarkedProcess] = []
        for entry in _PROC.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            process = _read(pid, name)
            if process is not None and matches(process.value):
                found.append(process)
        return tuple(found)

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """Signal through a pidfd opened before the identity is checked."""
        _require_procfs()

        def current() -> bool:
            return _read(process.pid, name) == process

        try:
            return signal_if_current(process.pid, signal.SIGKILL, current)
        except ProcessLookupError:
            return False


def _require_procfs() -> None:
    if not sys.platform.startswith("linux"):
        message = f"listing process environments needs Linux procfs, not {sys.platform}"
        raise ProcessTableUnavailableError(message)


def _read(pid: int, name: str) -> MarkedProcess | None:
    """The process *pid* if it is alive, readable and sets *name*; else ``None``."""
    try:
        environ = (_PROC / str(pid) / "environ").read_bytes()
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None  # gone, a zombie (no environment), or another user's
    prefix = f"{name}=".encode()
    for line in environ.split(b"\0"):
        if line.startswith(prefix):
            after_comm = stat.rpartition(")")[2].split()
            started = int(after_comm[_STAT_FIELDS_AFTER_COMM])
            return MarkedProcess(pid, started, line[len(prefix) :].decode(errors="replace"))
    return None
