"""The capacity gate of a client that no run holds: one bounded resend of a transient failure.

A run installs its own gate (``vs_runtime``'s quota policy) on every client it builds, and that
gate waits out an outage with its own backoff; nothing below it may retry, or the retry hides a
whole provider CLI attempt from the time the run credits to the hold (S179, #3300). A client no
run holds, the server's chat agent for one, has no such gate. Without one, a single overload (a
529 "overloaded") would end its turn, so a client starts with this gate and a run replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_agent.contracts import QuotaCondition

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_agent.contracts import AgentQuotaError, AgentTurnRequest, CapacityStops

UNGATED_RETRY_DELAYS_S: tuple[float, ...] = (30.0,)
"""Waits in seconds before each resend of a turn that stopped on a transient provider error.

One resend after 30 s, the retry agentshim made for every client before #3300 moved run turns
behind their gate. The provider CLI has already retried inside its attempt, so a failure that
outlasts this one resend is an outage the caller sees.
"""


class BoundedRetryCapacityGate:
    """Send a transient capacity stop again after each of ``delays``, then let it through.

    A transient stop is an outage (``QuotaCondition.UNAVAILABLE``) or sustained rate limiting
    (``RATE_LIMITED``): capacity may return at any moment. An exhausted usage quota
    (``QUOTA_EXHAUSTED``) cannot return within a short wait, so it is never sent again. The wait
    parks on the turn's :class:`~vs_agent.contracts.CapacityStops`, so cancelling the turn ends
    it at once, and the turn ends with its capacity error.
    """

    def __init__(self, delays: Sequence[float] = UNGATED_RETRY_DELAYS_S) -> None:
        """Resend a transient stop once per entry of ``delays``, waiting that many seconds."""
        if any(not 0 <= delay < float("inf") for delay in delays):
            message = f"retry delays must be finite and non-negative, got {tuple(delays)!r}"
            raise ValueError(message)
        self._delays = tuple(delays)

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, stops: CapacityStops
    ) -> None:
        """Return after this stop's delay, or raise ``error`` when no resend is left."""
        del turn, role
        if error.condition is QuotaCondition.QUOTA_EXHAUSTED or stops.count > len(self._delays):
            raise error
        stops.park(self._delays[stops.count - 1])
        if stops.cancelled:
            raise error

    def turn_served(self, provider: str, model: str | None) -> None:
        """Nothing to note: each turn's resends are counted on its own record."""
        del provider, model
