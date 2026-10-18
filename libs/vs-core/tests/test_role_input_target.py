"""An input for a role reaches exactly one turn of that role, once, and only a declared role.

A search declares the roles its turns run as, and core lists their role ids in the run's
declaration. An input addressed to one of them waits for that role's next session turn:
turns of other roles never take it, and the first matching turn that is accepted takes it
for good. An input or a steer naming a role the search does not declare is refused when
core receives it, since no turn could ever take it.
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import fake_search
from vs_sim.api.testing import examples

from .flow_support import Config, Plan
from .test_run_scoped_input_delivery import _fate_event, _with_inputs
from .test_session_inputs import occurrence
from .test_session_turns import invocation, reload_step, turn, waiting_turn_state

_Fate = Literal["deliver", "rejected", "failed", "cancelled", "crash"]
_ROLES = ("planner", "worker", "judge")
_NAMES = st.sampled_from(("planner", "worker", "judge", "critic", "editor"))


def _role(name: str, agent: str | None = None) -> core.Role:
    return core.Role(
        name=name,
        title=name.capitalize(),
        prompt=f"{name}.j2",
        reply=Plan,
        workspace=None,
        access=core.Access.READ_ONLY,
        turn_time=600.0,
        corrections=0,
        lost_turns=0,
        charge=core.Charge.TURN,
        agent=agent,
    )


async def _root(_ctx: object, _config: Config) -> None:
    return None


def _declared(*roles: str) -> core.CoreState:
    state = core.initial_state()
    declaration = state.run.declaration.model_copy(
        update={"roles": tuple(core.RoleId(root=role) for role in sorted(roles))}
    )
    return state.model_copy(
        update={"run": state.run.model_copy(update={"declaration": declaration})}
    )


def _role_turn(identity: str, role: str) -> core.TurnSpec:
    spec = turn(identity)
    session = spec.session.model_copy(update={"role_id": core.RoleId(root=role)})
    return spec.model_copy(update={"session": session})


@given(names=st.lists(_NAMES, min_size=1, max_size=5, unique=True), data=st.data())
def test_a_search_declares_the_role_id_of_every_role_it_asks(
    names: list[str], data: st.DataObject
) -> None:
    """A role that puts its question to another role's agent runs as that agent's role id."""
    roles = tuple(_role(name, agent=data.draw(st.sampled_from((None, names[0])))) for name in names)
    definition = fake_search("fake", Config, roles=roles)(_root)
    expected = sorted({definition.ids.role_id(role.identity).root for role in roles})
    assert [role.root for role in definition.declaration.roles] == expected


@given(
    declared=st.lists(st.sampled_from(_ROLES), unique=True),
    named=st.sampled_from(_ROLES),
    via=st.sampled_from(["input", "steer"]),
)
def test_core_refuses_a_role_its_search_does_not_declare(
    declared: list[str], named: str, via: str
) -> None:
    state = _declared(*declared)
    target = core.RoleInputTarget(role_id=core.RoleId(root=named))
    event: core.CoreEvent
    if via == "input":
        event = core.SessionInputReceived(input=occurrence(0, 0, target))
    else:
        control = core.ControlInput(
            control_id=core.ControlId(root="op-steer"),
            action="steer",
            artifact=core.ArtifactRef(artifact_id=core.ArtifactId(root="text"), digest="d"),
            target=target,
        )
        event = core.RunControlEvent(control=control, now_at=0.0)
    if named in declared:
        result = core.step(state, event)
        assert result.state.revision == state.revision + 1
    else:
        with pytest.raises(ValueError, match="not declared by the search"):
            core.step(state, event)


def test_only_a_steer_names_a_target() -> None:
    target = core.RoleInputTarget(role_id=core.RoleId(root="planner"))
    with pytest.raises(ValueError, match="only a steer has a target"):
        core.ControlInput(
            control_id=core.ControlId(root="c"),
            action="pause",
            owner=core.PauseOwner.OPERATOR,
            target=target,
        )


@settings(max_examples=examples(pr=5, nightly=100))
@given(
    turns=st.lists(
        st.tuples(
            st.sampled_from(_ROLES),
            st.sampled_from(["deliver", "rejected", "failed", "cancelled", "crash"]),
        ),
        max_size=8,
    )
)
def test_a_role_input_reaches_exactly_one_turn_of_that_role_once(
    turns: list[tuple[str, _Fate]],
) -> None:
    item = occurrence(0, 0, core.RoleInputTarget(role_id=core.RoleId(root="planner")))
    records = (core.InputRecord(input=item),)
    delivered: list[core.InputDelivered] = []
    for index, (role, fate) in enumerate(turns):
        spec = _role_turn(f"turn-{index}", role)
        ref = invocation(spec)
        reserved = reload_step(
            _with_inputs(waiting_turn_state(spec), records),
            core.InputReservationRequested(invocation=ref),
        )
        request = reserved.requests[0]
        assert isinstance(request, core.DispatchTurn)
        takes = role == "planner" and not delivered
        assert request.inputs == ((item,) if takes else ())
        event = _fate_event(fate, ref, request)
        after = reserved if event is None else reload_step(reserved.state, event)
        assert not [row for row in after.events if isinstance(row, core.InputDropped)]
        delivered.extend(row for row in after.events if isinstance(row, core.InputDelivered))
        records = after.state.sessions.inputs
        if event is None:
            # A crash before the outcome: recovery releases the reservation.
            records = tuple(
                row if row.receipt is not None else row.model_copy(update={"reserved_to": None})
                for row in records
            )
    took = [
        index for index, (role, fate) in enumerate(turns) if role == "planner" and fate == "deliver"
    ]
    assert [receipt.invocation.invocation_id.root for receipt in delivered] == [
        f"turn-{index}" for index in took[:1]
    ]


@given(
    roles=st.lists(st.sampled_from(["a", "b", "c"]), unique=True),
    other=st.lists(st.sampled_from(["a", "b", "c"]), unique=True),
    same_strategy=st.booleans(),
)
def test_a_declaration_resumes_under_any_roles_but_no_other_change(
    roles: list[str], other: list[str], *, same_strategy: bool
) -> None:
    def declared(strategy: str, names: list[str]) -> core.SearchDeclaration:
        return core.SearchDeclaration(
            strategy_id=core.StrategyId(root=strategy),
            roles=tuple(core.RoleId(root=name) for name in sorted(names)),
        )

    durable = declared("search", roles)
    offered = declared("search" if same_strategy else "other", other)

    assert durable.resumable_as(offered) == same_strategy
