"""Faults at the tool-call boundary: one wrapper over any tool dispatcher.

:class:`FaultyToolDispatch` sits between an agent and the tool server it
calls. It is keyed by tool name only and knows nothing of what a tool does.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import TYPE_CHECKING, cast

from vs_faults.plan import Boundary, FaultPlan, ToolFault

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class ToolCallFailedError(RuntimeError):
    """The agent's client reports a failed tool call (error, drop, or timeout)."""

    def __init__(self, tool: str, fault: ToolFault) -> None:
        """Name the tool and the injected fault."""
        self.tool = tool
        self.fault = fault
        super().__init__(f"tool call {tool} failed: {fault.value}")


type Dispatch = Callable[[str, Mapping[str, object]], dict[str, object]]


class FaultyToolDispatch:
    """Deliver tool calls through ``dispatch``, faulting the ones the plan schedules."""

    def __init__(self, dispatch: Dispatch, plan: FaultPlan) -> None:
        """Wrap ``dispatch(name, arguments)``; with no tool rules it is a pass-through."""
        self._dispatch = dispatch
        self._plan = plan
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.injected: list[tuple[str, int, ToolFault]] = []

    def __call__(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Call tool ``name``; raise :class:`ToolCallFailedError` where the agent sees a failure."""
        with self._lock:
            self._counts[name] += 1
            ordinal = self._counts[name]
            rule = self._plan.match(Boundary.TOOL_CALL, name, ordinal)
            # A rule's fault matches its boundary (FaultRule validates it).
            fault = cast("ToolFault | None", rule.fault if rule is not None else None)
            if fault is not None:
                self.injected.append((name, ordinal, fault))
        if fault is None:
            return self._dispatch(name, arguments)
        if fault is ToolFault.DUPLICATE:
            self._dispatch(name, arguments)
            return self._dispatch(name, arguments)
        if fault is ToolFault.TIMEOUT:
            self._dispatch(name, arguments)
        raise ToolCallFailedError(name, fault)
