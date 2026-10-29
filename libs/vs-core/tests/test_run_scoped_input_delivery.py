"""An input addressed to the run is delivered once, or waits; no turn's fate drops it.

The run outlives every attempt, so a reserving turn that ends, is cancelled or is rejected only
returns the input to the pending pool. A later invocation reserves it, and it is delivered by
exactly one of them.
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_attempt_registered_turn_proofs import registered_state
from .test_session_inputs import occurrence
from .test_session_review_fixes import release_after_closure
from .test_session_turns import (
    invocation,
    reload_step,
    scope,
    turn,
    turn_observation,
    waiting_turn_state,
)

_Disposition = Literal["park", "cancel", "settle"]
_Fate = Literal["deliver", "rejected", "failed", "cancelled", "crash"]


def _run_input() -> core.SessionInput:
    return occurrence(0, 0, core.ScopeInputTarget(scope=scope()))


def _with_inputs(state: core.CoreState, records: tuple[core.InputRecord, ...]) -> core.CoreState:
    return state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"inputs": records})}
    )


def _fate_event(
    fate: _Fate, ref: core.InvocationRef, request: core.Request
) -> core.CoreEvent | None:
    if fate == "crash":
        return None
    if fate == "deliver":
        return core.TurnObserved(
            invocation=ref,
            observation=turn_observation(
                request, accepted=True, terminal=True, status=core.ObservationStatus.SUCCEEDED
            ),
        )
    status = {
        "rejected": core.ObservationStatus.REJECTED,
        "failed": core.ObservationStatus.FAILED,
        "cancelled": core.ObservationStatus.CANCELLED,
    }[fate]
    return core.TurnObserved(
        invocation=ref,
        observation=turn_observation(request, accepted=False, terminal=True, status=status),
    )


@given(
    disposition=st.sampled_from(["park", "cancel", "settle"]),
    addressed_to=st.sampled_from(["run", "role"]),
)
def test_a_closed_reserver_returns_a_run_input_to_pending(
    disposition: _Disposition, addressed_to: str
) -> None:
    """Regression: a role's steer was dropped when the attempt whose turn reserved it closed.

    A role, like the run, outlives each of its attempts, so a later turn of the role takes it.
    """
    target: core.InputTarget = (
        core.ScopeInputTarget(scope=scope())
        if addressed_to == "run"
        else core.RoleInputTarget(role_id=core.RoleId(root="dynamic-judge"))
    )
    result = release_after_closure(disposition, target=target)
    record = result.state.sessions.inputs[0]
    assert record.receipt is None
    assert record.reserved_to is None
    assert not result.events


@settings(max_examples=examples(pr=3, nightly=100))
@given(fates=st.lists(st.sampled_from(["deliver", "rejected", "failed", "cancelled", "crash"])))
def test_a_run_input_is_delivered_exactly_once_or_stays_pending(fates: list[_Fate]) -> None:
    item = _run_input()
    records = (core.InputRecord(input=item),)
    delivered: list[core.InputDelivered] = []
    for index, fate in enumerate(fates):
        spec = turn(f"turn-{index}")
        ref = invocation(spec)
        state = _with_inputs(waiting_turn_state(spec), records)
        reserved = reload_step(state, core.InputReservationRequested(invocation=ref))
        request = reserved.requests[0]
        assert isinstance(request, core.DispatchTurn)
        assert request.inputs == ((item,) if not delivered else ())
        event = _fate_event(fate, ref, request)
        after = reserved if event is None else reload_step(reserved.state, event)
        assert not [row for row in after.events if isinstance(row, core.InputDropped)]
        delivered.extend(row for row in after.events if isinstance(row, core.InputDelivered))
        records = after.state.sessions.inputs
        if event is None:
            # A crash before the outcome: recovery finds the dispatched turn gone and the
            # reservation released, as the release observation would have recorded.
            records = tuple(
                row if row.receipt is not None else row.model_copy(update={"reserved_to": None})
                for row in records
            )
        assert len(delivered) <= 1
        assert len(records) == 1
        record = records[0]
        assert isinstance(record.receipt, core.InputDelivered) == (len(delivered) == 1)
        assert not isinstance(record.receipt, core.InputDropped)


@pytest.mark.parametrize("addressed_to", ["run", "invocation_scope"])
def test_only_a_session_turn_takes_a_run_input(addressed_to: str) -> None:
    """A registered operation reserves inputs for its own scope, never the run's."""
    state, _codec = registered_state(
        "exact",
        "current",
        "exact",
        (core.SessionPhase.ACQUIRING, core.ObservationStatus.UNKNOWN),
    )
    held = state.sessions.invocations[0]
    assert held.registered_operation is not None
    target = scope() if addressed_to == "run" else held.scope
    item = occurrence(0, 0, core.ScopeInputTarget(scope=target))
    state = _with_inputs(state, (core.InputRecord(input=item),))
    result = core.step(state, core.InputReservationRequested(invocation=held.invocation))
    reserved = result.state.sessions.inputs[0].reserved_to
    assert (reserved == held.invocation) == (addressed_to == "invocation_scope")
