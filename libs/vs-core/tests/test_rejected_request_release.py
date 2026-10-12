"""A closing attempt never waits on a workspace request that took nothing."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    AttemptPhase,
    CoreState,
    DecisionId,
    DispatchAuthorized,
    EventId,
    Observation,
    ObservationStatus,
    Request,
    RequestId,
    RequestObserved,
    RetireRequested,
    Scope,
    orphan_waits,
    step,
)

from .test_attempt_acquisition import acquiring_state, owner_ref

_VACANT = (ObservationStatus.REJECTED, ObservationStatus.FAILED, ObservationStatus.CANCELLED)


def _answer(request: Request, sequence: int) -> RequestObserved:
    """The executor's conclusive success for a request core asked it to run."""
    assert request.request_id is not None
    return RequestObserved(
        observation=Observation(
            event_id=EventId(root=f"answer-{sequence}"),
            request_id=request.request_id,
            scope=request.scope,
            sequence=1,
            observed_at=float(sequence),
            status=ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
            admission_id=request.admission_id,
        )
    )


def _drain(state: CoreState, requests: tuple[Request, ...]) -> CoreState:
    """Answer every request the core emits until it asks for nothing more."""
    pending = list(requests)
    for sequence in range(200):
        if not pending:
            return state
        request = pending.pop(0)
        assert request.request_id is not None
        state = step(state, DispatchAuthorized(request_id=request.request_id)).state
        result = step(state, _answer(request, sequence))
        state = result.state
        pending.extend(result.requests)
    pytest.fail("the attempt kept asking for work")


def _rejection(status: ObservationStatus) -> RequestObserved:
    return RequestObserved(
        observation=Observation(
            event_id=EventId(root="rejected"),
            request_id=RequestId(root="workspace"),
            scope=Scope(owner=owner_ref().attempt_id, generation=0),
            sequence=1,
            observed_at=1.0,
            status=status,
            accepted=False,
            terminal=True,
            released=False,
            admission_id=DecisionId(root="owner"),
        )
    )


def _retire() -> RetireRequested:
    """The stop path closing the attempt, independent of the workspace request's answer."""
    return RetireRequested(
        attempt=owner_ref(),
        disposition="cancel",
        authority=RequestId(root="stop-retirement"),
        admission_id=DecisionId(root="owner"),
        requested_at=1.0,
    )


@given(
    status=st.sampled_from(_VACANT),
    order=st.permutations(("rejection", "retirement")),
)
def test_a_workspace_request_answered_without_taking_anything_lets_the_attempt_close(
    status: ObservationStatus, order: list[str]
) -> None:
    """In either order, an answer that took nothing ends in a closed attempt, not a stall.

    The rejection arriving first drives setup failure into a closure; the retirement arriving
    first leaves the request as a release dependency of an already-closing attempt.
    """
    state = acquiring_state()
    requests: tuple[Request, ...] = ()
    for name in order:
        event = _rejection(status) if name == "rejection" else _retire()
        result = step(state, event)
        state, requests = result.state, (*requests, *result.requests)
    state = _drain(state, requests)
    assert state.attempts.attempts[0].phase == AttemptPhase.TERMINAL
    assert orphan_waits(state) == ()
