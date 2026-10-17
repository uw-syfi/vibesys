"""An interrupt-mode input joins a running turn when the provider takes it, else the next turn.

The shell offers a pending interrupt-mode input to a running session turn in two steps: it
commits ``InputOfferedMidTurn`` first and only then asks the provider, so a crash between
the two cannot lose the fact that the provider may have been asked. Whatever the provider
answers, and whenever the host crashes, the input reaches a turn at least once and no input
occurrence ever gets two receipts:

- the turn's acceptance delivers an offer the provider kept (``InputDelivered.mid_turn``);
- ``InputMidTurnReleased`` returns an offer that did not reach the turn to pending, so the
  next matching turn reserves it;
- a release after the turn already delivered it (the provider handed the message back at turn
  end) adds a next-turn copy, ``<id>.redelivery``, instead of losing the message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pytest

import vs_core.api as core

from .test_role_input_target import _role_turn
from .test_run_scoped_input_delivery import _fate_event, _with_inputs
from .test_session_inputs import occurrence
from .test_session_turns import invocation, reload_step, waiting_turn_state

_Fate = Literal["deliver", "rejected", "failed", "cancelled", "crash"]
_Answer = Literal["delivered", "unsupported", "no-running-turn"]
_Crash = Literal["none", "after-offer", "after-call"]
_Reject = Literal["never", "before-turn-end", "after-turn-end"]
_FATES: tuple[_Fate, ...] = ("deliver", "rejected", "failed", "cancelled", "crash")


def _interrupt(
    target: core.InputTarget | None = None, mode: core.InputMode = core.InputMode.INTERRUPT
) -> core.SessionInput:
    return core.SessionInput.model_validate(occurrence(0, 0, target).model_dump() | {"mode": mode})


@dataclass(frozen=True)
class _Offer:
    """What happens to one offer: the provider's answer, a late refusal and a host crash."""

    answer: _Answer
    reject: _Reject
    crash: _Crash

    @property
    def kept(self) -> bool:
        """Whether the shell saw the provider keep the message to the end of the turn."""
        return (
            self.crash == "none" and self.answer == "delivered" and self.reject != "before-turn-end"
        )


_OFFERS = tuple(
    _Offer(answer, reject, crash)
    for answer in ("delivered", "unsupported", "no-running-turn")
    for reject in ("never", "before-turn-end", "after-turn-end")
    for crash in ("none", "after-offer", "after-call")
    if answer == "delivered" or reject == "never"
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


def _released(ref: core.InvocationRef, input_id: core.InputId) -> core.InputMidTurnReleased:
    return core.InputMidTurnReleased(input_id=input_id, invocation=ref, now_at=0.0)


def _offer_to(
    run: _Run, state: core.CoreState, ref: core.InvocationRef, offer: _Offer
) -> tuple[core.CoreState, list[core.InputMidTurnReleased]]:
    """The shell's offer: commit, then (unless the host crashed) ask; release what missed."""
    late: list[core.InputMidTurnReleased] = []
    for candidate in core.mid_turn_offers(state):
        assert candidate.invocation == ref
        identity = candidate.input.input_id
        offered = core.InputOfferedMidTurn(input_id=identity, invocation=ref, now_at=0.0)
        state = run.record(reload_step(state, offered))
        (row,) = [r for r in state.sessions.inputs if r.input.input_id == identity]
        assert row.offered_to == ref
        release = _released(ref, identity)
        if offer.kept and offer.reject == "after-turn-end":
            late.append(release)
        elif not offer.kept:
            # Not taken, taken back, or a restarted host that cannot prove it was asked.
            state = run.record(reload_step(state, release))
    return state, late


def _turn(
    run: _Run, state: core.CoreState, spec: core.TurnSpec, fate: _Fate, offer: _Offer | None
) -> core.CoreState:
    """Run one turn; with ``offer`` the input arrives while it runs and the shell offers it."""
    ref = invocation(spec)
    reserved = reload_step(state, core.InputReservationRequested(invocation=ref))
    (request,) = reserved.requests
    assert isinstance(request, core.DispatchTurn)
    state = run.record(reserved)
    late: list[core.InputMidTurnReleased] = []
    if offer is not None:
        state = run.record(reload_step(state, core.SessionInputReceived(input=_interrupt())))
        state, late = _offer_to(run, state, ref, offer)
    event = _fate_event(fate, ref, request)
    if event is not None:
        state = run.record(reload_step(state, event))
    for release in late:
        state = run.record(reload_step(state, release))
    if event is None:
        # The host crashed with the turn: a restarted host releases every offer it finds.
        for row in state.sessions.inputs:
            if row.offered_to == ref and row.receipt is None:
                state = run.record(reload_step(state, _released(ref, row.input.input_id)))
    return state


@pytest.mark.parametrize("offer", _OFFERS, ids=str)
@pytest.mark.parametrize("fate", _FATES)
def test_an_interrupt_input_reaches_a_turn_at_least_once_with_one_receipt_per_occurrence(
    offer: _Offer, fate: _Fate
) -> None:
    """Every provider answer, late refusal, crash point and turn outcome, then one more turn."""
    run = _Run()
    first = _role_turn("turn-0", "planner")
    state = _turn(run, waiting_turn_state(first), first, fate, offer)
    second = _role_turn("turn-1", "planner")
    state = _with_inputs(waiting_turn_state(second), state.sessions.inputs)
    state = _turn(run, state, second, "deliver", None)
    records = state.sessions.inputs
    assert all(len(receipts) == 1 for receipts in run.receipts.values())
    delivered = {
        row.input.input_id.root: row.receipt
        for row in records
        if isinstance(row.receipt, core.InputDelivered)
    }
    assert [row for row in records if row.receipt is None] == []
    mid_turn = offer.kept and fate == "deliver"
    if mid_turn and offer.reject == "after-turn-end":
        # Taken into the turn, then handed back at its end: the next turn gets a copy.
        assert set(delivered) == {"input-0", "input-0.redelivery"}
        assert delivered["input-0"].mid_turn
        assert delivered["input-0.redelivery"].invocation == invocation(second)
    elif mid_turn:
        assert set(delivered) == {"input-0"}
        assert delivered["input-0"].mid_turn
        assert delivered["input-0"].invocation == invocation(first)
    else:
        # The offer missed (or a crash hid whether it landed): the next turn reserves it.
        assert set(delivered) == {"input-0"}
        assert not delivered["input-0"].mid_turn
        assert delivered["input-0"].invocation == invocation(second)


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
