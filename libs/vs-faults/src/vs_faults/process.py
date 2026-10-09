"""Faults at the process boundary: a wrapper over any agentshim ``CommandExecutor``.

:class:`FaultyExecutor` hands out the long-lived processes its inner executor
spawns, wrapped so the plan can break them the way a host, a container or a
provider CLI breaks: the process dies, goes silent, writes something that is not
a protocol message, or its container is replaced under it. It knows no
provider and no protocol; a rule names the ``n``-th stdout line the executor's
processes produced (a position in the exchange), and a sweep over ``n`` visits
every point of a turn.

An empty plan makes it a pass-through. One-shot ``run`` is not faulted: a
single request has no protocol to break mid-way.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

import agentshim
from agentshim import ProcessExited, StdoutLine

from vs_faults.plan import Boundary, FaultPlan, ProcessFault

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: What a process killed by a fault reports: the exit status of SIGKILL.
KILLED_STATUS = -9
#: What the corrupted line holds; no protocol message starts with it.
MALFORMED_LINE = "}{ truncated frame\n"


class FaultyExecutor:
    """A ``CommandExecutor`` whose spawned processes fail as the plan schedules.

    Lines are counted across every process this executor spawned, in the order
    they are read. ``injected`` records each fault that fired as
    ``(ordinal, fault)``; ``replacements`` counts container replacements.
    ``on_container_replaced`` runs when one fires, so a test can make the far
    end forget what lived in the old container.
    """

    def __init__(
        self,
        inner: agentshim.CommandExecutor,
        plan: FaultPlan,
        *,
        name: str = "agent",
        on_container_replaced: Callable[[], None] | None = None,
    ) -> None:
        """Wrap ``inner``; ``name`` is the rules' target for this executor."""
        self._inner = inner
        self._plan = plan
        self._name = name
        self._on_replaced = on_container_replaced
        self._lock = threading.Lock()
        self._lines = 0
        self._live: list[_FaultyProcess] = []
        self.injected: list[tuple[int, ProcessFault]] = []
        self.replacements = 0

    @property
    def lines(self) -> int:
        """How many stdout lines the executor's processes have produced so far.

        A fault-free run's final count is the number of positions a sweep over
        ``PROCESS_OUTPUT`` rules visits.
        """
        with self._lock:
            return self._lines

    def find_binary(self, name: str, env: Mapping[str, str]) -> str:
        """Delegate binary lookup."""
        return self._inner.find_binary(name, env)

    def check_binary(self, path: str, env: Mapping[str, str], *, timeout: float) -> None:
        """Delegate the health check."""
        self._inner.check_binary(path, env, timeout=timeout)

    def run(
        self, request: agentshim.CommandRequest, sink: agentshim.CommandStreamSink
    ) -> agentshim.CommandResult:
        """Delegate a one-shot command, unfaulted."""
        return self._inner.run(request, sink)

    def spawn(self, request: agentshim.SpawnRequest) -> agentshim.Process:
        """Spawn on the inner executor and wrap the process."""
        process = _FaultyProcess(
            self._inner.spawn(request), self._count_line, self._replace_container
        )
        with self._lock:
            self._live.append(process)
        return process

    def _count_line(self) -> ProcessFault | None:
        """Count one stdout line and return the fault scheduled for it, if any."""
        with self._lock:
            self._lines += 1
            ordinal = self._lines
            rule = self._plan.match(Boundary.PROCESS_OUTPUT, self._name, ordinal)
            fault = cast("ProcessFault | None", rule.fault if rule is not None else None)
            if fault is not None:
                self.injected.append((ordinal, fault))
            return fault

    def _replace_container(self) -> None:
        """Kill every live process at once, as replacing their container does."""
        with self._lock:
            victims = list(self._live)
            self._live.clear()
            self.replacements += 1
        for victim in victims:
            victim.die()
        if self._on_replaced is not None:
            self._on_replaced()


class _FaultyProcess:
    """One spawned process, faultable on the lines it writes."""

    def __init__(
        self,
        inner: agentshim.Process,
        count_line: Callable[[], ProcessFault | None],
        replace_container: Callable[[], None],
    ) -> None:
        self._inner = inner
        self._count_line = count_line
        self._replace_container = replace_container
        self._dead = False
        self._hung = False

    def die(self) -> None:
        """End the process; whatever it had not yet delivered is lost."""
        self._dead = True
        self._inner.kill()

    def write(self, data: str) -> None:
        """Write to stdin, or fail as a dead process does."""
        if self._dead:
            message = "the process was killed by an injected fault"
            raise agentshim.ProcessClosedError(message)
        self._inner.write(data)

    def close_stdin(self) -> None:
        """Close stdin."""
        self._inner.close_stdin()

    def next_output(self, timeout: float | None) -> agentshim.ProcessOutput | None:
        """Return the next item, unless a fault kills, silences or corrupts it."""
        if self._dead:
            return ProcessExited(KILLED_STATUS)
        if self._hung:
            # A silent process delivers nothing; stopping it is the caller's call.
            return None
        item = self._inner.next_output(timeout)
        if not isinstance(item, StdoutLine):
            return item
        fault = self._count_line()
        return item if fault is None else self._apply(fault)

    def _apply(self, fault: ProcessFault) -> agentshim.ProcessOutput | None:
        """What the reader gets in place of the line that ``fault`` strikes."""
        if fault is ProcessFault.HANG:
            self._hung = True
            return None
        if fault is ProcessFault.MALFORMED:
            return StdoutLine(MALFORMED_LINE)
        if fault is ProcessFault.CONTAINER_REPLACED:
            self._replace_container()
        self.die()
        return ProcessExited(KILLED_STATUS)

    def terminate(self) -> None:
        """Stop the process; a silent one ends too."""
        self._hung = False
        self._inner.terminate()

    def kill(self) -> None:
        """Stop the process now; a silent one ends too."""
        self._hung = False
        self._inner.kill()

    def wait(self, timeout: float | None) -> int | None:
        """Return the exit status once the process has ended."""
        if self._dead:
            return KILLED_STATUS
        return self._inner.wait(timeout)
