"""The agent processes inside a daemon's containers, as the sweep by step tag sees them.

A process is known by the container it runs in and the environment it started with, which
is all ``reap_step_agents`` reads of it (``/proc/<pid>/environ``). ``reap_tagged`` has the
signature of the sweep's default, so a test hands it to the reaper in place of ``docker exec``.
It matches the marker line exactly and kills nothing else, as the real sweep does.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.orphans import OrphanReapError
from vs_agent.step_tags import STEP_TAG_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_EVERY_CONTAINER = frozenset({"*"})
"""The marker ``unreachable()`` sets when no container is named; no container id is ``*``."""


@dataclass(frozen=True)
class FakeAgentProcess:
    """One process the fake daemon runs."""

    pid: int
    container_id: str
    env: Mapping[str, str]

    @property
    def tag(self) -> str | None:
        """The step that started the process, or ``None`` for one no step marked."""
        return self.env.get(STEP_TAG_ENV)


class FakeAgentProcesses:
    """A process table over containers: start processes, sweep them by tag, read who is alive."""

    def __init__(self) -> None:
        """Start with no process."""
        self._alive: dict[int, FakeAgentProcess] = {}
        self._next_pid = 1
        self._unreachable: frozenset[str] = frozenset()
        self.sweeps: list[tuple[str, str]] = []
        """Every ``reap_tagged(container_id, tag)`` call received, in order."""

    def start(self, container_id: str, env: Mapping[str, str]) -> FakeAgentProcess:
        """Start a process in *container_id* with *env* as its initial environment."""
        process = FakeAgentProcess(self._next_pid, container_id, dict(env))
        self._next_pid += 1
        self._alive[process.pid] = process
        return process

    @contextmanager
    def unreachable(self, *container_ids: str) -> Iterator[None]:
        """Make every sweep of the named containers fail while the context is open.

        With no container named, every container is unreachable. A sweep fails as it does
        against a daemon that cannot be reached.
        """
        self._unreachable = frozenset(container_ids) or _EVERY_CONTAINER
        try:
            yield
        finally:
            self._unreachable = frozenset()

    def alive(
        self, *, container_id: str | None = None, tag: str | None = None
    ) -> tuple[FakeAgentProcess, ...]:
        """The live processes, narrowed to one container and to the processes carrying *tag*."""
        return tuple(
            process
            for process in self._alive.values()
            if (container_id is None or process.container_id == container_id)
            and (tag is None or process.tag == tag)
        )

    def is_alive(self, process: FakeAgentProcess) -> bool:
        """Whether *process* still runs."""
        return process.pid in self._alive

    def reap_tagged(self, container_id: str, tag: str) -> None:
        """Kill every process of *container_id* whose environment carries exactly *tag*."""
        self.sweeps.append((container_id, tag))
        if self._unreachable == _EVERY_CONTAINER or container_id in self._unreachable:
            message = (
                f"could not end the agents of step {tag} in container {container_id}: unreachable"
            )
            raise OrphanReapError(message)
        for process in self.alive(container_id=container_id, tag=tag):
            del self._alive[process.pid]
