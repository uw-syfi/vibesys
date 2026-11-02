"""A request core ends without sending it tells its asker, whatever the kind of request.

Core ends a prepared request itself when it can never be sent: a request it depended on failed.
Nothing ran, so there is no outcome; the decision that asked still waits for an answer, and
a flow that awaits it never resumes if none comes. For every kind of registered operation, the
asker hears exactly one event of its decision, once, however many requests of the decision end.
"""

from __future__ import annotations

import pytest

import vs_core.api as core
from vs_core.api import DispatchAuthorized, ObservationStatus, RequestObserved, step

from .recovery_worlds import Kind, StepSpec, World, build


def _after_a_dependency_fails(kind: Kind) -> tuple[core.Transition, core.DecisionId]:
    """A step of ``kind`` that waits for another step, which then fails: what core publishes."""
    state, _ = build(World((StepSpec(100.0), StepSpec(100.0, kind=kind)), retries=0))
    first, second = state.records.records
    waiting = second.model_copy(
        update={"request": second.request.model_copy(update={"depends_on": (first.request_id,)})}
    )
    state = state.model_copy(
        update={
            "records": state.records.model_copy(update={"records": (first, waiting)}),
        }
    )
    state = step(state, DispatchAuthorized(request_id=first.request_id)).state
    ended = core.Observation(
        event_id=core.EventId(root="failed"),
        request_id=first.request_id,
        scope=first.request.scope,
        sequence=0,
        observed_at=1.0,
        status=ObservationStatus.FAILED,
        terminal=True,
        released=True,
        children_complete=True,
        admission_id=first.request.admission_id,
    )
    decision = waiting.request.decision_id
    assert decision is not None
    return step(state, RequestObserved(observation=ended)), decision


@pytest.mark.parametrize("kind", list(Kind))
def test_the_asker_of_a_step_cancelled_unsent_hears_of_it_once(kind: Kind) -> None:
    result, decision = _after_a_dependency_fails(kind)
    waiting = result.state.records.records[1]
    assert waiting.observation is not None
    assert waiting.observation.event_id.root.startswith("cancelled-unsent:")
    heard = [e for e in result.events if getattr(e, "decision", None) == decision]
    assert len(heard) == 1, result.events
    (failure,) = heard
    assert isinstance(failure, core.Failed)
    assert failure.reason is core.FailureReason.REFUSED
    assert failure.cause is core.RejectionCode.DEPENDENCY
