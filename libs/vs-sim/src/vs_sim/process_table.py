"""Find the processes that carry a mark, and end them by identity.

A mark is a ``NAME=value`` the launcher gave a process twice: in its environment, which its
descendants inherit, and as an argument of its leader, which ``ps`` shows. What a host can read
back differs, so the table that reads it differs:

- Linux lists every process whose environment sets the mark (``/proc/<pid>/environ``) and ends
  each one through a pidfd opened before its identity is checked, so a reused pid is never
  signalled.
- macOS shows no other process's environment, only its arguments. It lists the process group
  leaders whose arguments carry the mark and ends the leader's whole group. Its identity is the
  pid with the process's start second; there is no pidfd, so the check and the signal are two
  steps. A pid can be reused only after its whole group is gone and the kernel wraps its pid
  space, within one second of the check, and the signal then reaches a group that has the same
  pid *and* is a new leader. That is the guarantee, not an atomic one.

Only a definite absence means a process is gone (ENOENT, ESRCH). A process whose environment
this user may not read (EACCES) is another user's, which this host's launcher did not start, or
one of this user's own that cleared its dumpable flag (an ``ssh-agent``, a browser's sandbox);
the second is a residual (``docs/contributing/run-recovery.md``), not an outage, since waiting
would never end it. Any other failure to read a process (no file descriptors left, no memory, an
I/O error) is a host that cannot show its table, and raises
:class:`ProcessTableUnavailableError`: the work is then neither found nor proven gone.

A process is named by its pid and the kernel's start time of it, so a reused pid is a different
process. Work that left the group on macOS (a ``setsid`` child), or whose leader died first, is
not found: nothing marks it any more.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_sim.signals import signal_if_current

if TYPE_CHECKING:
    from collections.abc import Callable

_PROC = Path("/proc")
_STAT_FIELDS_AFTER_COMM = 20
"""Fields of ``/proc/<pid>/stat`` between the command's closing parenthesis and the start time."""
_EXIT_WAIT_S = 10.0
"""How long a kill waits for the process to exit; SIGKILL cannot be handled, so this bounds only a stuck kernel."""
_GROUP_POLL_S = 0.02
_PS = ("ps", "-axww", "-o", "pid=,pgid=,stat=,lstart=,command=")
_PS_TIMEOUT_S = 30.0
_PS_ROW = re.compile(
    r"^\s*(?P<pid>\d+)\s+(?P<pgid>\d+)\s+(?P<stat>\S+)\s+"
    r"(?P<started>\w{3} \w{3}\s+\d+ \d\d:\d\d:\d\d \d{4})\s+(?P<command>.*)$"
)


class ProcessTableUnavailableError(RuntimeError):
    """The host cannot list the marks of its processes."""


class ProcessSurvivedError(RuntimeError):
    """A process was sent SIGKILL and was still running when the wait for it ended."""


@dataclass(frozen=True, slots=True)
class MarkedProcess:
    """A live process that carries a mark, named so that a reused pid is a different process."""

    pid: int
    started: str
    """The kernel's start time of the process, in the form the host reports it."""
    value: str
    """What the mark was set to."""
    pgid: int = 0
    """The process group it leads or belongs to."""


class ProcessTable(Protocol):
    """The host's processes, by the marks they carry."""

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """The live processes this host can show carrying *name* with a value *matches* accepts.

        Which processes those are depends on the host (see the module docstring). Raises
        :class:`ProcessTableUnavailableError` when the host cannot list them.
        """
        ...

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """End *process* with SIGKILL iff the pid is still that process and still carries the mark.

        Where the host shows only a group's leader, the whole group ends. Returns whether the
        signal was sent: a process already gone, or a pid another process now holds, is left
        alone and reported ``False``. Raises :class:`ProcessSurvivedError` when what was
        signalled is still running after a bounded wait.
        """
        ...


class OsProcessTable:
    """The process table of the host this runs on: ``/proc`` on Linux, ``ps`` on macOS."""

    def __init__(self) -> None:
        """Choose the table the host's platform can read."""
        self._table: ProcessTable | None = (
            ProcfsProcessTable()
            if sys.platform.startswith("linux")
            else _PsProcessTable()
            if sys.platform == "darwin"
            else None
        )

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """List through the platform's table."""
        return self._available().marked(name, matches)

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """Kill through the platform's table."""
        return self._available().kill(process, name)

    def _available(self) -> ProcessTable:
        if self._table is None:
            message = f"no way to read process marks on {sys.platform}"
            raise ProcessTableUnavailableError(message)
        return self._table


class NoProcessTable:
    """A host with no table wired in: it cannot list marks, so nothing is ever reported gone.

    The default of a seam an entrypoint fills with :class:`OsProcessTable`. Asking it raises
    :class:`ProcessTableUnavailableError`, so a caller that waits on the table waits instead of
    concluding that no process is left.
    """

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """Refuse: there is no table to list."""
        del matches
        raise self._unavailable(name)

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """Refuse: there is no table to find *process* in."""
        del process
        raise self._unavailable(name)

    @staticmethod
    def _unavailable(name: str) -> ProcessTableUnavailableError:
        message = f"no process table was provided to read the {name} mark"
        return ProcessTableUnavailableError(message)


class ProcfsProcessTable:
    """Linux: every process whose environment sets the mark, read from a ``/proc`` tree."""

    def __init__(self, root: Path = _PROC) -> None:
        """Read the tree at *root*: ``/proc``, or a copy of its layout a test lays out."""
        self._root = root

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """Every live process of the tree whose environment sets *name* to a value *matches*."""
        try:
            entries = tuple(self._root.iterdir())
        except OSError as error:
            message = f"{self._root} cannot be listed: {error}"
            raise ProcessTableUnavailableError(message) from error
        found: list[MarkedProcess] = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            process = _read_procfs(self._root, int(entry.name), name)
            if process is not None and matches(process.value):
                found.append(process)
        return tuple(found)

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """SIGKILL *process* through a pidfd; raise if it is still marked after the bounded wait."""

        def current() -> bool:
            return _read_procfs(self._root, process.pid, name) == process

        try:
            signalled = signal_if_current(
                process.pid, signal.SIGKILL, current, exit_wait_s=_EXIT_WAIT_S
            )
        except ProcessLookupError:
            return False
        except PermissionError as error:
            message = f"process {process.pid} may not be signalled: {error}"
            raise ProcessTableUnavailableError(message) from error
        if signalled and current():
            message = f"process {process.pid} survived SIGKILL"
            raise ProcessSurvivedError(message)
        return signalled


def _read_procfs(root: Path, pid: int, name: str) -> MarkedProcess | None:
    """The process *pid* if it is alive, readable and sets *name*; else ``None``.

    Raises :class:`ProcessTableUnavailableError` when the process cannot be read for a reason
    that does not say it is gone or not this user's (see the module docstring).
    """
    try:
        environ = (root / str(pid) / "environ").read_bytes()
        stat = (root / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None  # gone, between the listing and the read
    except PermissionError:
        return None  # another user's, or this user's own that is not dumpable
    except OSError as error:
        message = f"process {pid} cannot be read: {error}"
        raise ProcessTableUnavailableError(message) from error
    prefix = f"{name}=".encode()
    for line in environ.split(b"\0"):
        if line.startswith(prefix):
            fields = stat.rpartition(")")[2].split()
            pgid = int(fields[2])
            started = fields[_STAT_FIELDS_AFTER_COMM]
            return MarkedProcess(pid, started, line[len(prefix) :].decode(errors="replace"), pgid)
    return None


@dataclass(frozen=True, slots=True)
class PsRow:
    """One process as ``ps`` lists it."""

    pid: int
    pgid: int
    stat: str
    started: str
    command: str


def parse_ps(listing: str) -> tuple[PsRow, ...]:
    """The rows of ``ps -axww -o pid=,pgid=,stat=,lstart=,command=``; other lines are skipped."""
    rows = (_PS_ROW.match(line) for line in listing.splitlines())
    return tuple(
        PsRow(int(m["pid"]), int(m["pgid"]), m["stat"], m["started"], m["command"])
        for m in rows
        if m is not None
    )


def mark_in(command: str, name: str) -> str | None:
    """The value of the ``name=value`` argument in a ``ps`` command column, if there is one."""
    found = re.search(rf"(?:^|\s){re.escape(name)}=(\S+)(?:\s|$)", command)
    return None if found is None else found[1]


class _PsProcessTable:
    """macOS: the group leaders whose arguments carry the mark; ending one ends its group."""

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        found: list[MarkedProcess] = []
        for row in _listing():
            value = mark_in(row.command, name) if row.pid == row.pgid else None
            if value is not None and matches(value) and not row.stat.startswith("Z"):
                found.append(MarkedProcess(row.pid, row.started, value, row.pgid))
        return tuple(found)

    def kill(self, process: MarkedProcess, name: str) -> bool:
        if process not in self.marked(name, lambda value: value == process.value):
            return False
        try:
            os.killpg(process.pgid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + _EXIT_WAIT_S
        while group_running(process.pgid):
            if time.monotonic() > deadline:
                message = f"process group {process.pgid} survived SIGKILL"
                raise ProcessSurvivedError(message)
            time.sleep(_GROUP_POLL_S)
        return True


def group_running(pgid: int) -> bool:
    """Whether process group ``pgid`` has a member that is not a zombie, as ``ps`` lists it.

    Raises :class:`ProcessTableUnavailableError` when ``ps`` cannot list the processes.
    """
    return any(row.pgid == pgid and not row.stat.startswith("Z") for row in _listing())


def _listing() -> tuple[PsRow, ...]:
    try:
        done = subprocess.run(  # noqa: S603  # lint-waiver: LW-914302 [S603]; the fixed argv of the host's own ps.
            _PS, capture_output=True, text=True, check=True, timeout=_PS_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as error:
        message = f"ps could not list the processes: {error}"
        raise ProcessTableUnavailableError(message) from error
    return parse_ps(done.stdout)
