"""Occurrence ordering, acceptance and retirement through the public kernel."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_turns import (
    invocation,
    reload_step,
    scope,
    turn,
    turn_observation,
    waiting_turn_state,
)
from .test_terminal_inputs import closing_state, drain


def occurrence(
    index: int, sequence: int, target: core.InputTarget | None = None
) -> core.SessionInput:
    return core.SessionInput(
        input_id=core.InputId(root=f"input-{index}"),
        target=target or core.ScopeInputTarget(scope=scope()),
        artifact=core.ArtifactRef(
            artifact_id=core.ArtifactId(root="equal-content"), digest="equal"
        ),
        received_at=0.0,
        sequence=sequence,
    )


@given(
    sequences=st.lists(st.integers(min_value=0, max_value=5), min_size=1, max_size=12),
    data=st.data(),
)
def test_occurrences_reserve_in_total_order_and_dispatch_once_across_reloads(
    sequences: list[int], data: st.DataObject
) -> None:
    spec = turn()
    state = waiting_turn_state(spec)
    inputs = tuple(occurrence(index, sequence) for index, sequence in enumerate(sequences))
    ordered = data.draw(st.permutations(inputs))
    for item in (*ordered, *ordered):
        state = reload_step(state, core.SessionInputReceived(input=item)).state
    sibling_before = state.sessions.model_copy(update={"inputs": ()})
    reserved = reload_step(state, core.InputReservationRequested(invocation=invocation(spec)))
    assert len(reserved.requests) == 1
    request = reserved.requests[0]
    assert isinstance(request, core.DispatchTurn)
    expected = tuple(sorted(inputs, key=lambda row: (row.sequence, row.input_id.root)))
    assert request.inputs == expected
    assert reserved.state.sessions.invocations[0].input_ids == tuple(
        row.input_id for row in expected
    )
    assert all(row.reserved_to == invocation(spec) for row in reserved.state.sessions.inputs)
    assert core.project(reserved.state).sessions[0].reserved_inputs == tuple(
        row.artifact for row in expected
    )
    assert reserved.state.attempts == state.attempts
    assert reserved.state.sessions.run_charges == sibling_before.run_charges
    replay = reload_step(
        reserved.state, core.InputReservationRequested(invocation=invocation(spec))
    )
    assert replay.requests == ()
    assert replay.state.sessions == reserved.state.sessions


@given(
    target=st.sampled_from(["scope", "item", "invocation", "foreign-scope", "foreign-invocation"]),
    generation=st.integers(min_value=1, max_value=4),
)
def test_reservation_never_silently_retargets_an_occurrence(target: str, generation: int) -> None:
    spec = turn()
    ref = invocation(spec)
    targets = {
        "scope": core.ScopeInputTarget(scope=scope()),
        "item": core.ItemInputTarget(item_id=core.ItemId(root="unowned")),
        "invocation": core.InvocationInputTarget(invocation=ref),
        "foreign-scope": core.ScopeInputTarget(
            scope=scope().model_copy(update={"generation": generation})
        ),
        "foreign-invocation": core.InvocationInputTarget(
            invocation=ref.model_copy(update={"generation": generation})
        ),
    }
    item = occurrence(0, 0, targets[target])
    state = reload_step(waiting_turn_state(spec), core.SessionInputReceived(input=item)).state
    result = reload_step(state, core.InputReservationRequested(invocation=ref))
    eligible = target in ("scope", "invocation")
    assert (result.state.sessions.inputs[0].reserved_to == ref) == eligible
    assert isinstance(result.requests[0], core.DispatchTurn)
    assert bool(result.requests[0].inputs) == eligible


@given(
    status=st.sampled_from(list(core.ObservationStatus)),
    accepted=st.booleans(),
    terminal=st.booleans(),
    target=st.sampled_from(["scope", "invocation"]),
)
def test_delivery_and_nonacceptance_are_separate_receipt_guards(
    status: core.ObservationStatus, *, accepted: bool, terminal: bool, target: str
) -> None:
    spec = turn()
    ref = invocation(spec)
    item = occurrence(
        0, 0, core.InvocationInputTarget(invocation=ref) if target == "invocation" else None
    )
    state = reload_step(waiting_turn_state(spec), core.SessionInputReceived(input=item)).state
    dispatched = reload_step(state, core.InputReservationRequested(invocation=ref))
    event = core.TurnObserved(
        invocation=ref,
        observation=turn_observation(
            dispatched.requests[0], accepted=accepted, terminal=terminal, status=status
        ),
    )
    result = reload_step(dispatched.state, event)
    record = result.state.sessions.inputs[0]
    delivered = accepted and status not in (
        core.ObservationStatus.UNKNOWN,
        core.ObservationStatus.PENDING,
    )
    released = (
        not accepted
        and terminal
        and status
        in (
            core.ObservationStatus.FAILED,
            core.ObservationStatus.REJECTED,
            core.ObservationStatus.CANCELLED,
        )
    )
    assert isinstance(record.receipt, core.InputDelivered) == delivered
    assert isinstance(record.receipt, core.InputDropped) == (released and target == "invocation")
    assert (record.reserved_to is None) == (released and target == "scope")
    if not delivered and not released:
        assert record == dispatched.state.sessions.inputs[0]
    if status == core.ObservationStatus.UNKNOWN or (
        status == core.ObservationStatus.SUCCEEDED and terminal and not accepted
    ):
        assert any(isinstance(row, core.InspectTurn) for row in result.requests)
    replay = reload_step(result.state, event)
    assert replay.state.sessions.inputs == result.state.sessions.inputs
    assert replay.events == ()


@given(orders=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=15))
def test_duplicate_reordered_stale_acceptance_never_redelivers(orders: list[int]) -> None:
    spec = turn()
    ref = invocation(spec)
    state = reload_step(
        waiting_turn_state(spec), core.SessionInputReceived(input=occurrence(0, 0))
    ).state
    dispatched = reload_step(state, core.InputReservationRequested(invocation=ref))
    base = turn_observation(
        dispatched.requests[0],
        accepted=True,
        terminal=True,
        status=core.ObservationStatus.SUCCEEDED,
    )
    state = dispatched.state
    receipts = []
    for version in (*orders, 3):
        event = core.TurnObserved(
            invocation=ref if version != 0 else ref.model_copy(update={"generation": 1}),
            observation=base.model_copy(update={"sequence": version + 1}),
        )
        result = reload_step(state, event)
        receipts.extend(row for row in result.events if isinstance(row, core.InputDelivered))
        state = result.state
    assert len(receipts) == 1
    assert state.sessions.inputs[0].receipt == receipts[0]
    assert state.sessions.run_charges == dispatched.state.sessions.run_charges


@given(count=st.integers(min_value=1, max_value=12), reserved=st.booleans())
def test_finish_run_drops_every_remaining_occurrence_before_run_ended(
    count: int, *, reserved: bool
) -> None:
    ref = invocation(turn())
    state = closing_state()
    records = tuple(
        core.InputRecord(input=occurrence(index, index), reserved_to=ref if reserved else None)
        for index in range(count)
    )
    state = state.model_copy(update={"sessions": core.SessionsState(inputs=records)})
    result = drain(state)
    assert len(result.events) == count + 1
    assert isinstance(result.events[-1], core.RunEnded)
    assert all(
        isinstance(row, core.InputDropped) and row.reason == core.InputDropReason.RUN_TERMINAL
        for row in result.events[:-1]
    )
    assert drain(result.state).events == ()
    assert state.sessions.inputs == records


@given(index=st.integers(min_value=0, max_value=100), conflict=st.booleans())
def test_occurrence_identity_is_idempotent_and_payload_conflicts_fail(
    index: int, *, conflict: bool
) -> None:
    item = occurrence(index, 0)
    state = reload_step(core.initial_state(), core.SessionInputReceived(input=item)).state
    changed = item.model_copy(update={"sequence": 1}) if conflict else item
    if conflict:
        with pytest.raises(core.ContractValidationError, match=r"input\.input_id"):
            reload_step(state, core.SessionInputReceived(input=changed))
    else:
        replay = reload_step(state, core.SessionInputReceived(input=changed))
        assert replay.state.sessions == state.sessions
        assert replay.events == ()
