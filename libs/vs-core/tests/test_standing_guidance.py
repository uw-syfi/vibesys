"""Standing guidance reaches every matching turn until it is withdrawn, then none.

A standing input is never reserved: core attaches it to each matching turn as the turn is
dispatched (``DispatchTurn.guidance``) and records which guidance each invocation was given.
No turn's outcome ends it. A withdraw control ends it with one ``WITHDRAWN`` receipt; a
withdrawal of guidance that has already ended changes nothing.
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_role_input_target import _role_turn
from .test_run_scoped_input_delivery import _fate_event, _with_inputs
from .test_session_inputs import occurrence
from .test_session_turns import invocation, reload_step, waiting_turn_state

_Fate = Literal["deliver", "rejected", "failed", "cancelled", "crash"]
_ROLES = ("planner", "worker")
_FATES = st.sampled_from(["deliver", "rejected", "failed", "cancelled", "crash"])


def _standing(target: core.InputTarget | None) -> core.SessionInput:
    return occurrence(0, 0, target).model_copy(update={"mode": core.InputMode.STANDING})


def _withdraw(state: core.CoreState, input_id: core.InputId, index: int) -> core.Transition:
    control = core.ControlInput(
        control_id=core.ControlId(root=f"op-w{index}"), action="withdraw", withdraws=input_id
    )
    return reload_step(state, core.RunControlEvent(control=control, now_at=1.0))


@settings(max_examples=examples(pr=5, nightly=100))
@given(
    addressed_to=st.sampled_from(["run", "planner"]),
    turns=st.lists(st.tuples(st.sampled_from(_ROLES), _FATES), max_size=8),
    withdrawn_after=st.integers(min_value=0, max_value=8),
    withdrawals=st.integers(min_value=1, max_value=2),
)
def test_standing_guidance_reaches_every_matching_turn_until_withdrawn(
    addressed_to: str,
    turns: list[tuple[str, _Fate]],
    withdrawn_after: int,
    withdrawals: int,
) -> None:
    target = (
        None if addressed_to == "run" else core.RoleInputTarget(role_id=core.RoleId(root="planner"))
    )
    item = _standing(target)
    records = (core.InputRecord(input=item),)
    receipts: list[core.InputDropped] = []
    for index, (role, fate) in enumerate(turns):
        spec = _role_turn(f"turn-{index}", role)
        state = _with_inputs(waiting_turn_state(spec), records)
        if index == withdrawn_after:
            for attempt in range(withdrawals):
                withdrawn = _withdraw(state, item.input_id, attempt)
                receipts += [e for e in withdrawn.events if isinstance(e, core.InputDropped)]
                # Only the inputs carry over: each turn is seeded on its own waiting state.
                state = _with_inputs(state, withdrawn.state.sessions.inputs)
        ref = invocation(spec)
        reserved = reload_step(state, core.InputReservationRequested(invocation=ref))
        (request,) = reserved.requests
        assert isinstance(request, core.DispatchTurn)
        matches = addressed_to == "run" or role == "planner"
        in_force = index < withdrawn_after
        assert request.inputs == ()
        assert request.guidance == ((item,) if matches and in_force else ())
        (held,) = [row for row in reserved.state.sessions.invocations if row.invocation == ref]
        assert held.guidance_ids == tuple(shown.input_id for shown in request.guidance)
        event = _fate_event(fate, ref, request)
        after = reserved if event is None else reload_step(reserved.state, event)
        # No turn's outcome ends standing guidance: only a withdrawal does.
        assert not [
            e for e in after.events if isinstance(e, (core.InputDelivered, core.InputDropped))
        ]
        records = after.state.sessions.inputs
    (record,) = records
    if withdrawn_after < len(turns):
        (receipt,) = receipts
        assert receipt.reason is core.InputDropReason.WITHDRAWN
        assert record.receipt == receipt
    else:
        assert receipts == []
        assert record.receipt is None


def test_a_withdrawal_names_standing_guidance_of_the_run() -> None:
    spec = _role_turn("turn-0", "planner")
    next_turn = occurrence(0, 0)
    state = _with_inputs(waiting_turn_state(spec), (core.InputRecord(input=next_turn),))
    with pytest.raises(core.ContractValidationError, match="withdraws"):
        _withdraw(state, next_turn.input_id, 0)
    with pytest.raises(core.ContractValidationError, match="withdraws"):
        _withdraw(state, core.InputId(root="input-unknown"), 0)
