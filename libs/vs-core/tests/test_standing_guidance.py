"""Standing guidance reaches every matching turn until it is withdrawn, then none.

A standing input is never reserved: core attaches it to each matching turn as the turn is
dispatched (``DispatchTurn.guidance``) and records which guidance each invocation was given.
No turn's outcome ends it. A withdraw control ends it with one ``WITHDRAWN`` receipt; a
withdrawal of guidance that has already ended changes nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_agent_turn_runs import ROLES, SCENARIOS, Scenario
from .test_role_input_target import _role_turn
from .test_run_scoped_input_delivery import _fate_event, _with_inputs
from .test_session_inputs import occurrence
from .test_session_turns import invocation, reload_step, waiting_turn_state
from .toy_agent_search import Config, Faults, ToyHost, run_toy

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


# PR CI draws only a handful of examples, so each edge of the schedule is pinned: a withdrawal
# before any turn, one after every turn (never withdrawn), and the idempotent double withdrawal,
# each for guidance to the run and to a role.
@settings(max_examples=examples(pr=5, nightly=100))
@example(addressed_to="run", turns=[("planner", "deliver")] * 3, withdrawn_after=0, withdrawals=1)
@example(
    addressed_to="planner", turns=[("planner", "failed")] * 3, withdrawn_after=1, withdrawals=2
)
@example(
    addressed_to="planner",
    turns=[("worker", "crash"), ("planner", "cancelled")],
    withdrawn_after=8,
    withdrawals=1,
)
@example(addressed_to="run", turns=[], withdrawn_after=0, withdrawals=2)
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


_TARGETS = st.sampled_from(["run", "toy-implementer", "toy-judge", "attempt", "item"])


def _toy_target(name: str) -> core.InputTarget | None:
    match name:
        case "run":
            return None
        case "attempt":
            return core.ScopeInputTarget(
                scope=core.Scope(owner=core.AttemptId(root="w0"), generation=0)
            )
        case "item":
            return core.ItemInputTarget(item_id=core.ItemId(root="w0"))
        case role:
            return core.RoleInputTarget(role_id=core.RoleId(root=role))


def _toy_matches(name: str, request: core.DispatchTurn | core.ResumeSessionTurn) -> bool:
    if name == "run":
        return True
    if name in ("attempt", "item"):
        return request.scope.owner.root == "w0"
    return request.turn.session.role_id.root == name


# A resumed turn (the first waits for its job) and a second workstream, so that guidance to
# attempt "w0" or item "w0" must skip the other workstream's turns.
_PINNED = Scenario(
    workstreams=2,
    in_flight=1,
    waits=(True,),
    polls=(1,),
    roles=ROLES,
    ends=("wait",),
    shares=True,
    faults=Faults(),
)


@settings(max_examples=examples(pr=4, nightly=40), deadline=None, derandomize=True, database=None)
# PR CI draws only a few of the 5 targets x 2 reload settings, so every one is pinned.
@example(scenario=_PINNED, target="run", reload=False)
@example(scenario=_PINNED, target="run", reload=True)
@example(scenario=_PINNED, target="toy-implementer", reload=False)
@example(scenario=_PINNED, target="toy-implementer", reload=True)
@example(scenario=_PINNED, target="toy-judge", reload=False)
@example(scenario=_PINNED, target="toy-judge", reload=True)
@example(scenario=_PINNED, target="attempt", reload=False)
@example(scenario=_PINNED, target="attempt", reload=True)
@example(scenario=_PINNED, target="item", reload=False)
@example(scenario=_PINNED, target="item", reload=True)
@given(scenario=SCENARIOS, target=_TARGETS, reload=st.booleans())
def test_standing_guidance_reaches_every_dispatched_and_resumed_turn_of_its_target(
    scenario: Scenario, target: str, *, reload: bool
) -> None:
    """On the real kernel: each fresh and resumed turn of the target gets it once, until it ends.

    Guidance to the run or a role ends when the run does; guidance to an attempt or an item
    ends when that attempt is drained, and no turn of it runs after.
    """
    item = _standing(_toy_target(target))
    faults = replace(scenario.faults, reload=reload)
    host = ToyHost(
        waits=scenario.waits,
        polls=scenario.polls,
        roles=scenario.roles,
        ends=scenario.ends,
        shares=scenario.shares,
        events=(core.SessionInputReceived(input=item),),
    )
    trace = run_toy(
        host, Config(workstreams=scenario.workstreams, in_flight=scenario.in_flight), faults=faults
    )
    turns = [
        r for r in trace.requests if isinstance(r, (core.DispatchTurn, core.ResumeSessionTurn))
    ]
    for request in turns:
        assert item not in request.inputs
        assert request.guidance == ((item,) if _toy_matches(target, request) else ())
    (record,) = [r for r in trace.core.sessions.inputs if r.input.input_id == item.input_id]
    assert isinstance(record.receipt, core.InputDropped)
    if target in ("attempt", "item"):
        assert record.receipt.reason in (
            core.InputDropReason.OWNER_TERMINAL,
            core.InputDropReason.OWNER_CANCELLED,
        )
    else:
        assert record.receipt.reason is core.InputDropReason.RUN_TERMINAL
