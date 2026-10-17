"""Interrupt-mode steers: offered to a running turn of their target now, else its next turn.

Core picks the running turn an interrupt-mode input may join (``mid_turn_offers``) and records
the offer (``InputOfferedMidTurn``) before anything reaches the provider, so a crash between
the two leaves an offer core can see. This component asks the provider only for an offer core
has committed, once per offer in this process, and tells core when the provider could not take
the message, or later took it back (``InputMidTurnReleased``). Core then gives the input to the
next turn, or appends a next-turn copy of it when the running turn had already accepted it; the
input's receipt says which happened.

Delivery is at least once: a host that crashed after asking the provider asks again, and a turn
that took a message back may already have read it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_agent.api import SteerOutcome
from vs_core.api import InputMidTurnReleased, InputOfferedMidTurn, MidTurnOffer, mid_turn_offers

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreEvent, CoreState, InputId, InvocationRef, SessionInput


class TurnSteering(Protocol):
    """Reaches a turn this process runs with a message while the turn is still running."""

    def offer_mid_turn(self, offer: MidTurnOffer, on_rejected: Callable[[], None]) -> SteerOutcome:
        """Offer ``offer``'s input to its running turn; ``on_rejected`` may follow ``DELIVERED``.

        ``on_rejected`` may be called from another thread, once, when the turn takes back a
        message it was given.
        """
        ...


class MidTurnSteering:
    """Offers each interrupt-mode input to a running turn of its target, at each control poll."""

    def __init__(self, steering: TurnSteering) -> None:
        """Offer through ``steering``; nothing has been asked yet in this process."""
        self._steering = steering
        self._asked: set[tuple[InputId, InvocationRef]] = set()
        self._declined: set[tuple[InputId, InvocationRef]] = set()
        # Appended from the provider's thread, drained by the loop: deque appends and pops
        # are atomic, and the loop is the only consumer.
        self._rejected: deque[tuple[InputId, InvocationRef]] = deque()

    def poll(self, core: CoreState, now_at: float) -> tuple[CoreEvent, ...]:
        """Events for core: offers to record, and offers the provider declined or took back.

        A turn that declined an input, or took it back, is not offered it again by this
        process: a turn core still holds as running but no longer runs here is asked once,
        not at every poll, and a turn that refuses a message is not sent it again.
        """
        events: list[CoreEvent] = []
        while self._rejected:
            input_id, invocation = self._rejected.popleft()
            self._declined.add((input_id, invocation))
            events.append(
                InputMidTurnReleased(input_id=input_id, invocation=invocation, now_at=now_at)
            )
        for record, offer in _committed(core):
            asked = (record.input.input_id, record.offered_to)
            if asked in self._asked:
                continue
            self._asked.add(asked)
            outcome = (
                SteerOutcome.NO_RUNNING_TURN
                if offer is None
                else self._steering.offer_mid_turn(offer, self._rejecter(asked))
            )
            if outcome is not SteerOutcome.DELIVERED:
                self._declined.add(asked)
                events.append(
                    InputMidTurnReleased(
                        input_id=record.input.input_id, invocation=record.offered_to, now_at=now_at
                    )
                )
        events.extend(
            InputOfferedMidTurn(
                input_id=offer.input.input_id, invocation=offer.invocation, now_at=now_at
            )
            for offer in mid_turn_offers(core)
            if (offer.input.input_id, offer.invocation) not in self._declined
        )
        return tuple(events)

    def _rejecter(self, asked: tuple[InputId, InvocationRef]) -> Callable[[], None]:
        def rejected() -> None:
            self._rejected.append(asked)

        return rejected


def _committed(core: CoreState) -> tuple[tuple[_Offered, MidTurnOffer | None], ...]:
    """Each offer core recorded that no turn outcome has settled yet, with its turn.

    The offer is None when core no longer holds the invocation it was made to.
    """
    invocations = {row.invocation: row for row in core.sessions.invocations}
    committed: list[tuple[_Offered, MidTurnOffer | None]] = []
    for record in core.sessions.inputs:
        if record.offered_to is None or record.receipt is not None:
            continue
        offered = _Offered(input=record.input, offered_to=record.offered_to)
        invocation = invocations.get(record.offered_to)
        committed.append(
            (
                offered,
                None
                if invocation is None
                else MidTurnOffer(
                    input=record.input, invocation=invocation.invocation, scope=invocation.scope
                ),
            )
        )
    return tuple(committed)


@dataclass(frozen=True)
class _Offered:
    input: SessionInput
    offered_to: InvocationRef


__all__ = ["MidTurnSteering", "TurnSteering"]
