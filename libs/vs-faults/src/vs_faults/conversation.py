"""Faults at the conversation boundary: a plan-driven wrapper over any agentshim ``Transport``.

:class:`FaultyTransport` opens the inner transport's conversations wrapped so a
turn the plan schedules fails the way a provider's transport reports it: a
classified provider error, a refused resume, a timeout, a process exit, or a
turn that ends in text that is not the reply. Recovery (retry, a fresh
conversation, renewal) is the session's job, so a faulted turn raises exactly
what the real transport would and the session above decides what happens.

The wrapper itself lives in ``vs_agent`` (the only package that imports
agentshim); this module supplies the plan lookup and the bookkeeping tests
assert on. A turn runs first and fails afterwards (like
:class:`~vs_faults.agent.FaultyAgentClient`), so the provider did the work
whose reply the caller loses: that is the case a recovery must not replay
blindly. An empty plan makes it a pass-through.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

from vs_agent.api.testing import (
    NOT_A_REPLY,
    TURN_BUDGET_S,
    ConversationFaultKind,
    FaultingTransport,
)
from vs_faults.plan import Boundary, ConversationFault, FaultPlan

if TYPE_CHECKING:
    from vs_agent.api.testing import Transport

__all__ = ["NOT_A_REPLY", "TURN_BUDGET_S", "FaultyTransport"]


class FaultyTransport(FaultingTransport):
    """A ``Transport`` whose conversations fail turns as the plan schedules.

    Turns are counted across every conversation this transport opened, so a
    rule names the ``n``-th turn of the session whichever process ran it.
    ``injected`` records each fault that fired as ``(ordinal, fault)``.
    """

    def __init__(self, inner: Transport, plan: FaultPlan, *, name: str = "agent") -> None:
        """Wrap ``inner``; ``name`` is the rules' target for this transport."""
        super().__init__(inner, self._next_kind)
        self._plan = plan
        self._name = name
        self._count_lock = threading.Lock()
        self._turns = 0
        self.injected: list[tuple[int, ConversationFault]] = []

    def _next_kind(self) -> ConversationFaultKind | None:
        # A plan's fault is a StrEnum whose values are the wrapper's kinds.
        return cast("ConversationFaultKind | None", self._next_planned_fault())

    def _next_planned_fault(self) -> ConversationFault | None:
        with self._count_lock:
            self._turns += 1
            ordinal = self._turns
            rule = self._plan.match(Boundary.CONVERSATION_TURN, self._name, ordinal)
            fault = cast("ConversationFault | None", rule.fault if rule is not None else None)
            if fault is not None:
                self.injected.append((ordinal, fault))
            return fault
