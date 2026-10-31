"""An in-memory process table, for tests that find and end marked processes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_sim.process_table import MarkedProcess, ProcessTableUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeProcessTable:
    """Processes that carry marks, by pid, each with the start time that names it."""

    def __init__(self) -> None:
        """Start with no process."""
        self._live: dict[int, tuple[int, dict[str, str]]] = {}
        self._clock = 0
        self.killed: list[MarkedProcess] = []
        self.unreadable = False

    def start(self, name: str, value: str, *, pid: int | None = None) -> MarkedProcess:
        """A new process whose environment sets *name* to *value*.

        *pid* names a pid to take over from its last holder, as the kernel reuses one.
        """
        taken = pid if pid is not None else max(self._live, default=1000) + 1
        self._clock += 1
        self._live[taken] = (self._clock, {name: value})
        return MarkedProcess(taken, self._clock, value)

    def exit(self, process: MarkedProcess) -> None:
        """The process ends by itself."""
        self._live.pop(process.pid, None)

    def alive(self, process: MarkedProcess) -> bool:
        """Whether this very process (not a later holder of its pid) is running."""
        held = self._live.get(process.pid)
        return held is not None and held[0] == process.started

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        """The live processes whose mark *name* *matches* accepts."""
        if self.unreadable:
            raise ProcessTableUnavailableError
        return tuple(
            MarkedProcess(pid, started, marks[name])
            for pid, (started, marks) in sorted(self._live.items())
            if name in marks and matches(marks[name])
        )

    def kill(self, process: MarkedProcess, name: str) -> bool:
        """End the process if the pid is still it and still carries the mark."""
        held = self._live.get(process.pid)
        if held is None or held[0] != process.started or name not in held[1]:
            return False
        del self._live[process.pid]
        self.killed.append(process)
        return True
