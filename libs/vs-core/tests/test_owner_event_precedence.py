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

from .test_measurements import observation as observation_of
from .test_measurements import requested
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


def submitted_job() -> tuple[core.CoreState, core.SubmitMeasurement]:
    """A measurement submitted and dispatched, whose own view saw the job still running."""
    result = requested()
    submit = result.requests[0]
    assert isinstance(submit, core.SubmitMeasurement)
    assert submit.request_id is not None
    state = reload_step(result.state, core.DispatchAuthorized(request_id=submit.request_id)).state
    first = job_observation(submit, 1)
    state = reload_step(state, core.RequestObserved(observation=first)).state
    return state, submit


def job_observation(
    submit: core.SubmitMeasurement, sequence: int, status: core.ObservationStatus | None = None
) -> core.Observation:
    status = status or core.ObservationStatus.PENDING
    terminal = status not in (core.ObservationStatus.PENDING, core.ObservationStatus.UNKNOWN)
    return observation_of(submit, sequence, status=status, terminal=terminal, released=terminal)


def submit_phase(state: core.CoreState, submit: core.SubmitMeasurement) -> core.IntentPhase:
    return next(row.phase for row in state.intents.intents if row.request_id == submit.request_id)


conclusive = st.sampled_from(
    [
        core.ObservationStatus.SUCCEEDED,
        core.ObservationStatus.FAILED,
        core.ObservationStatus.CANCELLED,
    ]
)


@given(running=st.integers(0, 4), ending=conclusive, replays=st.integers(0, 2))
def test_a_submit_intent_completes_exactly_when_its_job_is_terminal(
    running: int, ending: core.ObservationStatus, replays: int
) -> None:
    """The job's own observations, whichever event carries them, decide the submit's state."""
    state, submit = submitted_job()
    assert submit_phase(state, submit) == core.IntentPhase.DISPATCHED
    resource = core.ResourceId(root="job")
    sequence = 1
    for _ in range(running):
        sequence += 1
        view = job_observation(submit, sequence)
        state = reload_step(state, core.JobObserved(resource_id=resource, observation=view)).state
        assert submit_phase(state, submit) == core.IntentPhase.DISPATCHED
    sequence += 1
    view = job_observation(submit, sequence, ending)
    for _ in range(1 + replays):
        state = reload_step(state, core.JobObserved(resource_id=resource, observation=view)).state
    assert submit_phase(state, submit) == core.IntentPhase.COMPLETED
    held = next(row for row in state.intents.intents if row.request_id == submit.request_id)
    assert held.observation == view
