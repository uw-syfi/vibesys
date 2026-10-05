"""A request's completion is decided by every event that knows more, not the first to arrive.

An executor answers a plain turn with two events: the request's observation, which
the intent ledger commits and forwards, and the owner's `TurnObserved`, the only
carrier of the reply. The result the strategy sees must carry the reply for every
interleaving of the two, including duplicates and a restart between them.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_turns import (
    invocation,
    reload_state,
    reload_step,
    turn,
    waiting_turn_state,
    with_intent,
)

REPLY = '{"candidate": "rev-1"}'
SCHEMA = core.SchemaRef(name="plan", version=1)


def dispatched() -> tuple[core.CoreState, core.DispatchTurn]:
    spec = turn()
    reserved = reload_step(
        waiting_turn_state(spec), core.InputReservationRequested(invocation=invocation(spec))
    )
    request = next(row for row in reserved.requests if isinstance(row, core.DispatchTurn))
    state = with_intent(reserved.state, request)
    intents = tuple(
        row.model_copy(update={"lifecycle": core.LifecycleClass.SESSION_TURN})
        for row in state.intents.intents
    )
    update = {"intents": state.intents.model_copy(update={"intents": intents})}
    return state.model_copy(update=update), request


def observation(request: core.DispatchTurn) -> core.Observation:
    assert request.request_id is not None
    return core.Observation(
        event_id=core.EventId(root="observation-1"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=core.ResourceId(root="conversation"),
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )


def test_the_reply_survives_the_observation_arriving_first() -> None:
    state, request = dispatched()
    seen = observation(request)
    state = reload_step(state, core.RequestObserved(observation=seen)).state
    result = reload_step(
        state,
        core.TurnObserved(
            invocation=invocation(request.turn),
            observation=seen,
            output_schema=SCHEMA,
            output_json=REPLY,
        ),
    )
    (published,) = (row for row in result.events if isinstance(row, core.TurnResult))
    assert published.output_json == REPLY


def test_a_failed_turn_needs_no_owner_event_to_be_published() -> None:
    """Only a success owes a reply; a refusal is complete on the observation alone."""
    for status in (core.ObservationStatus.FAILED, core.ObservationStatus.CANCELLED):
        state, request = dispatched()
        seen = observation(request).model_copy(update={"status": status, "accepted": False})
        result = reload_step(state, core.RequestObserved(observation=seen))
        (published,) = (row for row in result.events if isinstance(row, core.TurnResult))
        assert published.observation.status == status
        assert published.output_json is None


type Delivery = tuple[str, bool]
deliveries = st.lists(
    st.tuples(st.sampled_from(["observation", "owner"]), st.booleans()), min_size=2, max_size=8
).filter(lambda plan: {kind for kind, _ in plan} == {"observation", "owner"})


@given(plan=deliveries)
def test_the_published_turn_carries_the_reply_for_every_delivery_order(
    plan: list[Delivery],
) -> None:
    """Either order, duplicates, and a restart (reload) before any delivery.

    The observation is the ledger's disposition; the owner event is the reply. However
    they interleave, exactly one result is published and it carries the reply.
    """
    state, request = dispatched()
    seen = observation(request)
    owner = core.TurnObserved(
        invocation=invocation(request.turn),
        observation=seen,
        output_schema=SCHEMA,
        output_json=REPLY,
    )
    published: list[core.TurnResult] = []
    for kind, restart in plan:
        if restart:
            state = reload_state(state)
        event = core.RequestObserved(observation=seen) if kind == "observation" else owner
        result = core.step(state, event)
        state = result.state
        published.extend(row for row in result.events if isinstance(row, core.TurnResult))
    assert [row.output_json for row in published] == [REPLY]
    (invocation_row,) = state.sessions.invocations
    assert invocation_row.output_json == REPLY
