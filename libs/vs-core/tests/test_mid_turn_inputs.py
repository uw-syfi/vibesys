"""An interrupt-mode input joins a running turn when the provider takes it, else the next turn.

The shell offers a pending interrupt-mode input to a running session turn in steps it commits
one by one: ``InputOfferedMidTurn`` before it asks the provider, then ``InputTakenMidTurn``
once the provider answers that it took the message. A crash between any two of them cannot
lose the input. Whatever the provider answers, whenever the host crashes, and in whichever
order a restarted host reports the offer and the turn's end, the input reaches a turn at least
once and no input occurrence ever gets two receipts:

- the turn's acceptance delivers an offer the provider took (``InputDelivered.mid_turn``);
- an offer the turn ends without having taken waits for the next turn, as does one the
  turn's result refused (``InputMidTurnReleased.refused``, committed before the acceptance);
- ``InputMidTurnReleased`` returns any other offer that did not reach the turn to pending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pytest

import vs_core.api as core

from .test_role_input_target import _role_turn
from .test_run_scoped_input_delivery import _fate_event, _with_inputs
from .test_session_inputs import occurrence
from .test_session_turns import (
    invocation,
    reload_step,
    waiting_for_reservation,
    waiting_turn_state,
)
from .test_terminal_inputs import closing_state, drain

_Fate = Literal["deliver", "rejected", "failed", "cancelled", "crash"]
_Answer = Literal["delivered", "unsupported", "no-running-turn"]
_Crash = Literal["none", "after-offer", "after-call", "after-taken"]
_Order = Literal["release-first", "turn-end-first"]
_FATES: tuple[_Fate, ...] = ("deliver", "rejected", "failed", "cancelled", "crash")


def _interrupt(
    target: core.InputTarget | None = None, mode: core.InputMode = core.InputMode.INTERRUPT
) -> core.SessionInput:
    return core.SessionInput.model_validate(occurrence(0, 0, target).model_dump() | {"mode": mode})


@dataclass(frozen=True)
class _Offer:
    """What happens to one offer: the provider's answer, a refusal, a crash, and replay order.

    ``refused`` is a refusal the turn's result carries. ``crash`` stops the host after a step
    of the offer; the restarted host releases the offer it finds, before or after it reports
    the turn's end (``order``), and the turn's own result is all that survives of the call.
    """

    answer: _Answer
    refused: bool
    crash: _Crash
    order: _Order

    @property
    def taken(self) -> bool:
        """Whether core committed that the provider took the message."""
        return self.answer == "delivered" and self.crash in ("none", "after-taken")

    def joins(self, fate: _Fate) -> bool:
        """Whether the input joins the running turn instead of reaching the next one."""
        if fate != "deliver" or not self.taken or self.refused:
            return False
        return self.crash == "none" or self.order == "turn-end-first"


_OFFERS = tuple(
    _Offer(answer, refused, crash, order)
    for answer in ("delivered", "unsupported", "no-running-turn")
    for refused in (False, True)
    for crash in ("none", "after-offer", "after-call", "after-taken")
    for order in ("release-first", "turn-end-first")
    if (answer == "delivered" or (not refused and crash in ("none", "after-offer")))
    and (crash != "none" or order == "release-first")
    and (not refused or crash == "none")
)


@dataclass
class _Run:
    """Every receipt core issued, by input occurrence."""

    receipts: dict[str, list[core.InputReceipt]] = field(default_factory=dict)

    def record(self, result: core.Transition) -> core.CoreState:
        for event in result.events:
            if isinstance(event, core.InputDelivered | core.InputDropped):
                self.receipts.setdefault(event.input_id.root, []).append(event)
        for row in result.state.sessions.inputs:
            # Validate each record as a reload would, whether or not this run reloads.
            core.InputRecord.model_validate(row.model_dump())
        return result.state


def _released(
    ref: core.InvocationRef, input_id: core.InputId, *, refused: bool = False
) -> core.InputMidTurnReleased:
    return core.InputMidTurnReleased(input_id=input_id, invocation=ref, now_at=0.0, refused=refused)


def _offer_to(
    run: _Run, state: core.CoreState, ref: core.InvocationRef, offer: _Offer
) -> core.CoreState:
    """The shell's offer: commit it, ask (unless the host died first), commit the answer."""
    for candidate in core.mid_turn_offers(state):
        assert candidate.invocation == ref
        identity = candidate.input.input_id
        offered = core.InputOfferedMidTurn(input_id=identity, invocation=ref, now_at=0.0)
        state = run.record(reload_step(state, offered))
        (row,) = [r for r in state.sessions.inputs if r.input.input_id == identity]
        assert row.offered_to == ref
        if offer.crash == "after-offer":
            continue
        if offer.answer != "delivered":
            state = run.record(reload_step(state, _released(ref, identity)))
        elif offer.taken:
            taken = core.InputTakenMidTurn(input_id=identity, invocation=ref, now_at=0.0)
            state = run.record(reload_step(state, taken))
    return state


def _restart_releases(run: _Run, state: core.CoreState, ref: core.InvocationRef) -> core.CoreState:
    """A restarted host cannot reach the turn an earlier host offered to: it releases each."""
    for row in state.sessions.inputs:
        if row.offered_to == ref and row.receipt is None:
            state = run.record(reload_step(state, _released(ref, row.input.input_id)))
    return state


def _turn(
    run: _Run, state: core.CoreState, spec: core.TurnSpec, fate: _Fate, offer: _Offer | None
) -> core.CoreState:
    """Run one turn; with ``offer`` the input arrives while it runs and the shell offers it."""
    ref = invocation(spec)
    reserved = reload_step(state, core.InputReservationRequested(invocation=ref))
    (request,) = reserved.requests
    assert isinstance(request, core.DispatchTurn)
    state = run.record(reserved)
    if offer is not None:
        state = run.record(reload_step(state, core.SessionInputReceived(input=_interrupt())))
        state = _offer_to(run, state, ref, offer)
    crashed = offer is not None and offer.crash != "none"
    if crashed and offer is not None and offer.order == "release-first":
        state = _restart_releases(run, state, ref)
    event = _fate_event(fate, ref, request)
    if event is not None:
        if offer is not None and offer.refused:
            # The turn's result carries the refusal, committed before its acceptance.
            identity = _interrupt().input_id
            state = run.record(reload_step(state, _released(ref, identity, refused=True)))
        state = run.record(reload_step(state, event))
    if crashed or event is None:
        state = _restart_releases(run, state, ref)
    return state


@pytest.mark.parametrize("offer", _OFFERS, ids=str)
@pytest.mark.parametrize("fate", _FATES)
def test_an_interrupt_input_reaches_a_turn_at_least_once_with_one_receipt_per_occurrence(
    offer: _Offer, fate: _Fate
) -> None:
    """Every provider answer, refusal, crash point, replay order and turn outcome, then a turn."""
    run = _Run()
    first = _role_turn("turn-0", "planner")
    state = _turn(run, waiting_turn_state(first), first, fate, offer)
    second = _role_turn("turn-1", "planner")
    state = _with_inputs(waiting_turn_state(second), state.sessions.inputs)
    state = _turn(run, state, second, "deliver", None)
    records = state.sessions.inputs
    assert all(len(receipts) == 1 for receipts in run.receipts.values())
    assert [row for row in records if row.receipt is None] == []
    (row,) = records
    assert isinstance(row.receipt, core.InputDelivered)
    if offer.joins(fate):
        assert row.receipt.mid_turn
        assert row.receipt.invocation == invocation(first)
    else:
        # The offer missed, was refused, or a crash hid whether it landed: the next turn takes it.
        assert not row.receipt.mid_turn
        assert row.receipt.invocation == invocation(second)


def test_a_turn_that_ends_before_the_provider_takes_its_offer_leaves_it_for_the_next() -> None:
    """The turn's acceptance commits right after the offer, before the shell asked anyone."""
    run = _Run()
    first = _role_turn("turn-0", "planner")
    ref = invocation(first)
    state = reload_step(waiting_turn_state(first), core.InputReservationRequested(invocation=ref))
    (request,) = state.requests
    state = run.record(state)
    state = run.record(reload_step(state, core.SessionInputReceived(input=_interrupt())))
    item = _interrupt()
    offer = core.InputOfferedMidTurn(input_id=item.input_id, invocation=ref, now_at=0.0)
    state = run.record(reload_step(state, offer))
    ended = _fate_event("deliver", ref, request)
    assert ended is not None
    state = run.record(reload_step(state, ended))
    (row,) = state.sessions.inputs
    assert (row.offered_to, row.reserved_to, row.receipt) == (None, None, None)
    # A take the shell commits after the turn ended changes nothing.
    late = core.InputTakenMidTurn(input_id=item.input_id, invocation=ref, now_at=0.0)
    assert reload_step(state, late).state.sessions == state.sessions
    second = _role_turn("turn-1", "planner")
    state = _turn(
        run,
        _with_inputs(waiting_turn_state(second), state.sessions.inputs),
        second,
        "deliver",
        None,
    )
    (row,) = state.sessions.inputs
    assert isinstance(row.receipt, core.InputDelivered)
    assert row.receipt.invocation == invocation(second)
    assert not row.receipt.mid_turn


def _running_with_offer(*, taken: bool, refused: bool = False) -> core.CoreState:
    """A run whose one running turn holds an interrupt input's offer, as the shell left it."""
    spec = _role_turn("turn-0", "planner")
    ref = invocation(spec)
    item = _interrupt()
    state = reload_step(waiting_turn_state(spec), core.InputReservationRequested(invocation=ref))
    state = reload_step(state.state, core.SessionInputReceived(input=item))
    offer = core.InputOfferedMidTurn(input_id=item.input_id, invocation=ref, now_at=0.0)
    state = reload_step(state.state, offer)
    if taken:
        take = core.InputTakenMidTurn(input_id=item.input_id, invocation=ref, now_at=0.0)
        state = reload_step(state.state, take)
    return reload_step(state.state, _released(ref, item.input_id, refused=refused)).state


_TAKEN_CASES = {
    "never-taken": (False, False, core.InputDropReason.RUN_TERMINAL),
    "taken": (True, False, core.InputDropReason.DELIVERY_UNKNOWN),
    "taken-then-refused": (True, True, core.InputDropReason.RUN_TERMINAL),
}


@pytest.mark.parametrize("case", sorted(_TAKEN_CASES))
def test_an_input_a_provider_took_that_no_turn_confirms_is_dropped_as_unknown(case: str) -> None:
    """A host died after the provider took the message, and no later turn can confirm it.

    The drop says the agent may have read it, unless the turn refused it.
    """
    taken, refused, expected = _TAKEN_CASES[case]
    state = _running_with_offer(taken=taken, refused=refused)
    (row,) = state.sessions.inputs
    assert row.possibly_read is (taken and not refused)
    assert row.taken_by is None
    assert (row.offered_to, row.receipt) == (None, None)
    closing = closing_state()
    closing = closing.model_copy(
        update={"sessions": core.SessionsState(inputs=state.sessions.inputs)}
    )
    (row,) = drain(closing).state.sessions.inputs
    assert isinstance(row.receipt, core.InputDropped)
    assert row.receipt.reason is expected


def test_a_parallel_turn_never_reserves_an_input_offered_to_another() -> None:
    """Two turns of one role run at once; an input offered to the first is not the second's."""
    first, other = _role_turn("turn-0", "planner"), _role_turn("turn-1", "planner")
    session = other.session.model_copy(update={"session_id": core.SessionId(root="session-1")})
    second = other.model_copy(update={"session": session})
    # The second turn waits on a live request, so core does not end it as stalled meanwhile.
    a, b = waiting_turn_state(first), waiting_for_reservation(second)
    state = a.model_copy(
        update={
            "records": b.records,
            "sessions": a.sessions.model_copy(
                update={
                    "sessions": (*a.sessions.sessions, *b.sessions.sessions),
                    "invocations": (*a.sessions.invocations, *b.sessions.invocations),
                    "run_charges": (*a.sessions.run_charges, *b.sessions.run_charges),
                }
            ),
        }
    )
    state = reload_step(state, core.InputReservationRequested(invocation=invocation(first))).state
    item = _interrupt()
    state = reload_step(state, core.SessionInputReceived(input=item)).state
    (offer,) = core.mid_turn_offers(state)
    assert offer.invocation == invocation(first)
    state = reload_step(
        state,
        core.InputOfferedMidTurn(input_id=item.input_id, invocation=offer.invocation, now_at=0.0),
    ).state
    reserved = reload_step(state, core.InputReservationRequested(invocation=invocation(second)))
    (request,) = reserved.requests
    assert isinstance(request, core.DispatchTurn)
    assert item not in request.inputs
    (row,) = reserved.state.sessions.inputs
    assert (row.offered_to, row.reserved_to) == (invocation(first), None)


def test_an_offer_core_cannot_record_changes_nothing() -> None:
    spec = _role_turn("turn-0", "planner")
    ref = invocation(spec)
    item = _interrupt()
    idle = _with_inputs(waiting_turn_state(spec), (core.InputRecord(input=item),))
    offer = core.InputOfferedMidTurn(input_id=item.input_id, invocation=ref, now_at=0.0)
    # No turn runs yet: the next turn reserves it instead.
    assert core.mid_turn_offers(idle) == ()
    assert reload_step(idle, offer).state.sessions.inputs == idle.sessions.inputs
    running = reload_step(idle, core.InputReservationRequested(invocation=ref)).state
    (reserved,) = running.sessions.inputs
    assert reserved.reserved_to == ref
    assert reload_step(running, offer).state.sessions == running.sessions
    # A release of an offer core never recorded changes nothing either.
    assert reload_step(running, _released(ref, item.input_id)).state.sessions == running.sessions


def test_an_offer_twice_and_a_release_twice_settle_once() -> None:
    spec = _role_turn("turn-0", "planner")
    ref = invocation(spec)
    state = reload_step(
        waiting_turn_state(spec), core.InputReservationRequested(invocation=ref)
    ).state
    state = reload_step(state, core.SessionInputReceived(input=_interrupt())).state
    item = _interrupt()
    offer = core.InputOfferedMidTurn(input_id=item.input_id, invocation=ref, now_at=0.0)
    once = reload_step(state, offer).state
    assert reload_step(once, offer).state.sessions == once.sessions
    released = reload_step(once, _released(ref, item.input_id)).state
    assert reload_step(released, _released(ref, item.input_id)).state.sessions == released.sessions
    (row,) = released.sessions.inputs
    assert (row.offered_to, row.reserved_to, row.receipt) == (None, None, None)


@pytest.mark.parametrize("mode", [core.InputMode.NEXT_TURN, core.InputMode.STANDING, None])
def test_only_an_interrupt_input_of_the_run_is_offered(mode: core.InputMode | None) -> None:
    spec = _role_turn("turn-0", "planner")
    ref = invocation(spec)
    records = () if mode is None else (core.InputRecord(input=_interrupt(mode=mode)),)
    state = _with_inputs(waiting_turn_state(spec), records)
    with pytest.raises(core.ContractValidationError, match="interrupt-mode"):
        reload_step(
            state,
            core.InputOfferedMidTurn(input_id=_interrupt().input_id, invocation=ref, now_at=0.0),
        )
    with pytest.raises(core.ContractValidationError, match="interrupt-mode"):
        reload_step(state, _released(ref, _interrupt().input_id))


def test_an_interrupt_input_cannot_name_one_invocation() -> None:
    spec = _role_turn("turn-0", "planner")
    target = core.InvocationInputTarget(invocation=invocation(spec))
    with pytest.raises(ValueError, match="interrupt input is for a run"):
        _interrupt(target)


def _two_running_planner_turns() -> tuple[
    core.CoreState, tuple[core.InvocationRef, core.Request], tuple[core.InvocationRef, core.Request]
]:
    """A run with two turns of one role running at once, each dispatched, with no input yet."""
    first, other = _role_turn("turn-0", "planner"), _role_turn("turn-1", "planner")
    session = other.session.model_copy(update={"session_id": core.SessionId(root="session-1")})
    second = other.model_copy(update={"session": session})
    a, b = waiting_turn_state(first), waiting_for_reservation(second)
    state = a.model_copy(
        update={
            "records": b.records,
            "sessions": a.sessions.model_copy(
                update={
                    "sessions": (*a.sessions.sessions, *b.sessions.sessions),
                    "invocations": (*a.sessions.invocations, *b.sessions.invocations),
                    "run_charges": (*a.sessions.run_charges, *b.sessions.run_charges),
                }
            ),
        }
    )
    running = []
    for spec in (first, second):
        reserved = reload_step(state, core.InputReservationRequested(invocation=invocation(spec)))
        (request,) = reserved.requests
        assert isinstance(request, core.DispatchTurn)
        state = reserved.state
        running.append((invocation(spec), request))
    return state, running[0], running[1]


def test_a_take_by_a_turn_that_failed_never_delivers_the_offer_to_another_turn() -> None:
    """Regression: a stale take marked an untaken offer to a second running turn as delivered.

    Turn A's provider takes the message and A fails. The message is offered to turn B, which
    runs alongside, and B ends before the shell asks B's provider. B never got the message, so
    it waits for the next turn, marked as possibly read by A.
    """
    run = _Run()
    state, *turns = _two_running_planner_turns()
    item = _interrupt()
    state = run.record(reload_step(state, core.SessionInputReceived(input=item)))
    (offer,) = core.mid_turn_offers(state)
    took, other = turns if turns[0][0] == offer.invocation else turns[::-1]
    for event in (
        core.InputOfferedMidTurn(input_id=item.input_id, invocation=took[0], now_at=0.0),
        core.InputTakenMidTurn(input_id=item.input_id, invocation=took[0], now_at=0.0),
    ):
        state = run.record(reload_step(state, event))
    failed = _fate_event("failed", *took)
    assert failed is not None
    state = run.record(reload_step(state, failed))
    (offer,) = core.mid_turn_offers(state)
    assert offer.invocation == other[0]
    offered = core.InputOfferedMidTurn(input_id=item.input_id, invocation=other[0], now_at=0.0)
    state = run.record(reload_step(state, offered))
    ended = _fate_event("deliver", *other)
    assert ended is not None
    state = run.record(reload_step(state, ended))

    (row,) = state.sessions.inputs
    assert row.receipt is None, "a turn that never took the message was told it received it"
    assert (row.offered_to, row.reserved_to, row.taken_by) == (None, None, None)
    assert row.possibly_read
    assert run.receipts == {}
