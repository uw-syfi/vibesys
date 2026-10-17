"""Interrupt-mode steers: offered to a running turn of their target now, else its next turn.

Core picks the running turn an interrupt-mode input may join (``mid_turn_offers``) and records
the offer (``InputOfferedMidTurn``) before anything reaches the provider, so a crash between
the two leaves an offer core can see. This component asks the provider only for an offer core
has committed, once per offer in this process. When the provider takes the message it commits
that fact (``InputTakenMidTurn``); when it does not, it tells core (``InputMidTurnReleased``).
Only a taken offer joins the turn when the turn's acceptance commits: an offer the turn ends
without having taken, or one the turn's result refused, waits for the next turn.

Delivery is at least once: a host that died after the provider took a message, before it
committed that, leaves an untaken offer, and the next turn gets the message again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vs_agent.api import SteerOutcome
from vs_core.api import (
    InputMidTurnReleased,
    InputOfferedMidTurn,
    InputTakenMidTurn,
    MidTurnOffer,
    mid_turn_offers,
)

if TYPE_CHECKING:
    from vs_core.api import CoreEvent, CoreState, InputId, InvocationRef


class TurnSteering(Protocol):
    """Reaches a turn this process runs with a message while the turn is still running."""

    def offer_mid_turn(self, offer: MidTurnOffer) -> SteerOutcome:
        """Offer ``offer``'s input to its running turn.

        A ``DELIVERED`` message the provider later refuses is named in the turn's result,
        which releases it ahead of the turn's acceptance.
        """
        ...


class MidTurnSteering:
    """Offers each interrupt-mode input to a running turn of its target, at each control poll."""

    def __init__(self, steering: TurnSteering) -> None:
        """Offer through ``steering``; nothing has been asked yet in this process."""
        self._steering = steering
        self._asked: set[tuple[InputId, InvocationRef]] = set()
        self._declined: set[tuple[InputId, InvocationRef]] = set()

    def poll(self, core: CoreState, now_at: float) -> tuple[CoreEvent, ...]:
        """Events for core: offers to record, and the provider's answers to committed ones.

        A turn that declined an input is not offered it again by this process: a turn core
        still holds as running but no longer runs here is asked once, not at every poll.
        """
        events: list[CoreEvent] = []
        for input_id, invocation, running in _committed(core):
            asked = (input_id, invocation)
            if asked in self._asked:
                continue
            self._asked.add(asked)
            outcome = (
                SteerOutcome.NO_RUNNING_TURN
                if running is None
                else self._steering.offer_mid_turn(running)
            )
            if outcome is SteerOutcome.DELIVERED:
                events.append(
                    InputTakenMidTurn(input_id=input_id, invocation=invocation, now_at=now_at)
                )
            else:
                self._declined.add(asked)
                events.append(
                    InputMidTurnReleased(input_id=input_id, invocation=invocation, now_at=now_at)
                )
        events.extend(
            InputOfferedMidTurn(
                input_id=offer.input.input_id, invocation=offer.invocation, now_at=now_at
            )
            for offer in mid_turn_offers(core)
            if (offer.input.input_id, offer.invocation) not in self._declined
        )
        return tuple(events)


def _committed(
    core: CoreState,
) -> tuple[tuple[InputId, InvocationRef, MidTurnOffer | None], ...]:
    """Each offer core recorded that no answer has settled yet, with its running turn.

    The turn is None when core no longer holds the invocation the offer was made to.
    """
    invocations = {row.invocation: row for row in core.sessions.invocations}
    committed: list[tuple[InputId, InvocationRef, MidTurnOffer | None]] = []
    for record in core.sessions.inputs:
        if record.offered_to is None or record.receipt is not None:
            continue
        invocation = invocations.get(record.offered_to)
        running = (
            None
            if invocation is None
            else MidTurnOffer(
                input=record.input, invocation=invocation.invocation, scope=invocation.scope
            )
        )
        committed.append((record.input.input_id, record.offered_to, running))
    return tuple(committed)


__all__ = ["MidTurnSteering", "TurnSteering"]
