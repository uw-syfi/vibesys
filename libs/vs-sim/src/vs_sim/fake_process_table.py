"""An in-memory process table that shows only what its modelled host shows."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from vs_sim.process_table import MarkedProcess, ProcessSurvivedError, ProcessTableUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable


class Shows(StrEnum):
    """What a host's process listing reveals about a process's mark."""

    ENVIRONMENT = "environment"
    """Linux: every process whose environment sets the mark, descendants included."""
    LEADER_ARGUMENTS = "leader-arguments"
    """macOS: only a process group leader whose arguments carry the mark."""


class FakeProcessTable:
    """Processes that carry marks, each named by pid and start, listed the way a host lists them.

    A stage started by :meth:`start_stage` carries its mark in its environment and, on its
    leader, in its arguments, as the real launcher gives both. What :meth:`marked` returns is
    only what the modelled host shows, so a test cannot pass by seeing a process the host would
    hide; :meth:`running` is the ground truth a test asserts on.
    """

    def __init__(self, shows: Shows = Shows.ENVIRONMENT) -> None:
        """Model a host that shows *shows*."""
        self._shows = shows
        self._live: dict[int, _Process] = {}
        self._clock = 0
        self.killed: list[MarkedProcess] = []
        self.unreadable = False
        self.survivors = False
        """A kill signals but the group does not end: a stuck kernel."""
        self.respawning = False
        """A supervisor outside the mark starts a new marked process after every kill."""
        self.unsignalable = False
        """The host refuses the signal (``EPERM``) for a process it listed."""

    def start_stage(self, name: str, value: str, *, pid: int | None = None) -> MarkedProcess:
        """A new group leader marked *name*=*value*; *pid* takes over a pid from its last holder."""
        taken = pid if pid is not None else max(self._live, default=1000) + 1
        self._clock += 1
        self._live[taken] = _Process(str(self._clock), taken, {name: value}, leader=True)
        return MarkedProcess(taken, str(self._clock), value, taken)

    def spawn(self, parent: MarkedProcess, *, new_session: bool = False) -> MarkedProcess:
        """A child of *parent*: it inherits the mark and, unless *new_session*, the group."""
        held = self._live[parent.pid]
        pid = max(self._live, default=1000) + 1
        self._clock += 1
        group = pid if new_session else held.pgid
        self._live[pid] = _Process(str(self._clock), group, dict(held.environment), leader=False)
        return MarkedProcess(pid, str(self._clock), parent.value, group)

    def exit(self, process: MarkedProcess) -> None:
        """The process ends by itself; the rest of its group goes on."""
        self._live.pop(process.pid, None)

    def alive(self, process: MarkedProcess) -> bool:
        """Whether this very process (not a later holder of its pid) is running."""
        held = self._live.get(process.pid)
        return held is not None and held.started == process.started

    def running(self, name: str, matches: Callable[[str], bool]) -> int:
        """How many live processes truly carry the mark, shown by the host or not."""
        return sum(
            1 for p in self._live.values() if name in p.environment and matches(p.environment[name])
        )

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """The live processes the modelled host shows carrying *name*."""
        if self.unreadable:
            raise ProcessTableUnavailableError
        return tuple(
            MarkedProcess(pid, p.started, p.environment[name], p.pgid)
            for pid, p in sorted(self._live.items())
            if name in p.environment
            and matches(p.environment[name])
            and (self._shows is Shows.ENVIRONMENT or (p.leader and p.pgid == pid))
        )

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """End the process (on a leader-showing host, its whole group) if the pid is still it."""
        held = self._live.get(process.pid)
        if held is None or held.started != process.started or name not in held.environment:
            return False
        group = held.pgid
        doomed = (
            [process.pid]
            if self._shows is Shows.ENVIRONMENT
            else [pid for pid, p in self._live.items() if p.pgid == group]
        )
        if self.unsignalable:
            raise ProcessTableUnavailableError
        if self.survivors:
            raise ProcessSurvivedError
        for pid in doomed:
            del self._live[pid]
        self.killed.append(process)
        if self.respawning:
            self.start_stage(name, held.environment[name])
        return True


class _Process:
    __slots__ = ("environment", "leader", "pgid", "started")

    def __init__(
        self, started: str, pgid: int, environment: dict[str, str], *, leader: bool
    ) -> None:
        self.started = started
        self.pgid = pgid
        self.environment = environment
        self.leader = leader
