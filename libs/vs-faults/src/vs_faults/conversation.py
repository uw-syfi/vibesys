"""Faults at the conversation boundary: a wrapper over any agentshim ``Transport``.

:class:`FaultyTransport` opens the inner transport's conversations wrapped so a
turn the plan schedules fails the way a provider's transport reports it: a
classified provider error, a refused resume, a timeout, a process exit, or a
turn that ends in text that is not the reply. Recovery (retry, a fresh
conversation, renewal) is the session's job, so a faulted turn raises exactly
what the real transport would and the session above decides what happens.

A turn runs first and fails afterwards (like :class:`~vs_faults.agent.FaultyAgentClient`),
so the provider did the work whose reply the caller loses: that is the case a
recovery must not replay blindly. An empty plan makes it a pass-through.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import agentshim

from vs_faults.plan import Boundary, ConversationFault, FaultPlan

if TYPE_CHECKING:
    from collections.abc import Callable

TURN_BUDGET_S = 3600.0
"""The turn budget a timed-out turn reports exceeding, as the agent client fault does."""

NOT_A_REPLY = "I could not produce the requested reply."
"""What a malformed turn answers with: prose where a structured reply was due."""


class FaultyTransport:
    """A ``Transport`` whose conversations fail turns as the plan schedules.

    Turns are counted across every conversation this transport opened, so a
    rule names the ``n``-th turn of the session whichever process ran it.
    ``injected`` records each fault that fired as ``(ordinal, fault)``.
    """

    def __init__(self, inner: agentshim.Transport, plan: FaultPlan, *, name: str = "agent") -> None:
        """Wrap ``inner``; ``name`` is the rules' target for this transport."""
        self._inner = inner
        self._plan = plan
        self._name = name
        self._lock = threading.Lock()
        self._turns = 0
        self.injected: list[tuple[int, ConversationFault]] = []

    @property
    def profile(self) -> agentshim.ProviderProfile:
        """The inner transport's profile."""
        return self._inner.profile

    def open(self, spec: agentshim.ConversationSpec) -> agentshim.Conversation:
        """Open the inner conversation and wrap it."""
        return _FaultyConversation(self._inner.open(spec), self._next_fault)

    def _next_fault(self) -> tuple[int, ConversationFault | None]:
        with self._lock:
            self._turns += 1
            ordinal = self._turns
            rule = self._plan.match(Boundary.CONVERSATION_TURN, self._name, ordinal)
            fault = cast("ConversationFault | None", rule.fault if rule is not None else None)
            if fault is not None:
                self.injected.append((ordinal, fault))
            return ordinal, fault


class _FaultyConversation:
    """One conversation whose turns the plan may fail."""

    def __init__(
        self,
        inner: agentshim.Conversation,
        next_fault: Callable[[], tuple[int, ConversationFault | None]],
    ) -> None:
        self._inner = inner
        self._next_fault = next_fault

    @property
    def conversation_id(self) -> str | None:
        """The inner conversation's id."""
        return self._inner.conversation_id

    def turn(
        self, request: agentshim.TurnRequest, emit: Callable[[agentshim.AgentEvent], None]
    ) -> agentshim.TurnResult:
        """Run the turn, then lose its reply as the plan says."""
        _ordinal, fault = self._next_fault()
        result = self._inner.turn(request, emit)
        if fault is None:
            return result
        return _lose(fault, result, self._inner.conversation_id)

    def interrupt(self) -> None:
        """Interrupt the inner conversation's turn."""
        self._inner.interrupt()

    def steer(self, text: str) -> None:
        """Steer the inner conversation, which must be steerable."""
        if not isinstance(self._inner, agentshim.SteerableConversation):
            message = "the wrapped conversation cannot take a message mid-turn"
            raise agentshim.ProviderCapabilityError(message)
        self._inner.steer(text)

    def close(self) -> None:
        """Close the inner conversation."""
        self._inner.close()


def _lose(
    fault: ConversationFault,
    result: agentshim.TurnResult,
    conversation_id: str | None,
) -> agentshim.TurnResult:
    """Return the failure (or the corrupted result) ``fault`` makes of a finished turn."""
    argv = ("agent",)
    if fault is ConversationFault.MALFORMED:
        return replace(result, text=NOT_A_REPLY, structured_output=None)
    if fault is ConversationFault.TIMEOUT:
        raise agentshim.TurnTimeoutError(TURN_BUDGET_S)
    if fault is ConversationFault.RESUME_REFUSED:
        raise agentshim.SessionResumeError(argv, 1, conversation_id or "unknown")
    if fault is ConversationFault.EXITED:
        raise agentshim.CliExitError(argv, -9, stderr="killed")
    kind = (
        agentshim.FailureKind.TRANSIENT
        if fault is ConversationFault.TRANSIENT
        else agentshim.FailureKind.OTHER
    )
    message = f"injected {fault.value} turn failure"
    raise agentshim.TurnFailedError(message, kind=kind)
