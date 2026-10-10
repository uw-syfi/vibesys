"""Faults at the process boundary: a plan-driven wrapper over an agentshim ``CommandExecutor``.

:class:`FaultyExecutor` hands out the long-lived processes its inner executor
spawns, wrapped so the plan can break them the way a host, a container or a
provider CLI breaks: the process dies, goes silent, writes something that is not
a protocol message, or its container is replaced under it. It knows no
provider and no protocol; a rule names the ``n``-th stdout line the executor's
processes produced (a position in the exchange), and a sweep over ``n`` visits
every point of a turn.

The wrapper itself lives in ``vs_agent`` (the only package that imports
agentshim); this module supplies the plan lookup and the bookkeeping tests
assert on. An empty plan makes it a pass-through.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

from vs_agent.fault_injection import (
    KILLED_STATUS,
    MALFORMED_LINE,
    FaultingExecutor,
    ProcessFaultKind,
)
from vs_faults.api import Boundary, FaultPlan, ProcessFault

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.fault_injection import CommandExecutor

__all__ = ["KILLED_STATUS", "MALFORMED_LINE", "FaultyExecutor"]


class FaultyExecutor(FaultingExecutor):
    """A ``CommandExecutor`` whose spawned processes fail as the plan schedules.

    Lines are counted across every process this executor spawned, in the order
    they are read. ``injected`` records each fault that fired as
    ``(ordinal, fault)``; ``replacements`` counts container replacements.
    ``on_container_replaced`` runs when one fires, so a test can make the far
    end forget what lived in the old container.
    """

    def __init__(
        self,
        inner: CommandExecutor,
        plan: FaultPlan,
        *,
        name: str = "agent",
        on_container_replaced: Callable[[], None] | None = None,
    ) -> None:
        """Wrap ``inner``; ``name`` is the rules' target for this executor."""
        super().__init__(inner, self._next_kind, on_container_replaced=on_container_replaced)
        self._plan = plan
        self._name = name
        self._count_lock = threading.Lock()
        self._lines = 0
        self.injected: list[tuple[int, ProcessFault]] = []

    @property
    def lines(self) -> int:
        """How many stdout lines the executor's processes have produced so far.

        A fault-free run's final count is the number of positions a sweep over
        ``PROCESS_OUTPUT`` rules visits.
        """
        with self._count_lock:
            return self._lines

    def _next_kind(self) -> ProcessFaultKind | None:
        # A plan's fault is a StrEnum whose values are the wrapper's kinds.
        return cast("ProcessFaultKind | None", self._count_line())

    def _count_line(self) -> ProcessFault | None:
        """Count one stdout line and return the fault scheduled for it, if any."""
        with self._count_lock:
            self._lines += 1
            ordinal = self._lines
            rule = self._plan.match(Boundary.PROCESS_OUTPUT, self._name, ordinal)
            fault = cast("ProcessFault | None", rule.fault if rule is not None else None)
            if fault is not None:
                self.injected.append((ordinal, fault))
            return fault
