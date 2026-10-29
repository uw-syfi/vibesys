"""A steer's life reaches the run log as IntentState records a client can tell apart."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from vibesys.api import SteerDelivery, SteerDeliveryState, SteerMode
from vibesys.records.api import MappingState, map_event

_AT = datetime(2026, 1, 1, tzinfo=UTC)

# state -> (core event, step, reason code a client reads the state back from)
STATES = {
    SteerDeliveryState.QUEUED: (e.CoreEventType.STEER_QUEUED, p.IntentStep.APPLIED, None),
    SteerDeliveryState.DROPPED: (e.CoreEventType.STEER_DROPPED, p.IntentStep.REFUSED, "dropped"),
    SteerDeliveryState.ENDED: (e.CoreEventType.GUIDANCE_ENDED, p.IntentStep.APPLIED, "ended"),
    SteerDeliveryState.REFUSED: (e.CoreEventType.CONTROL_REFUSED, p.IntentStep.REFUSED, None),
}


@pytest.mark.regression
@example(state=SteerDeliveryState.DROPPED, control="control-a", reason="not delivered")
@given(
    state=st.sampled_from(sorted(STATES, key=str)),
    control=st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True),
    reason=st.text(min_size=1, max_size=20),
)
def test_each_delivery_state_is_one_distinguishable_intent_outcome(
    state: SteerDeliveryState, control: str, reason: str
) -> None:
    kind, step, code = STATES[state]
    delivery = SteerDelivery(
        state=state,
        control_id=control,
        mode=SteerMode.NEXT_TURN if state == SteerDeliveryState.QUEUED else None,
        reason=None if state == SteerDeliveryState.QUEUED else reason,
    )
    observation = e.CoreEvent.model_validate(
        {
            "type": kind,
            "data": e.SteerDeliveryData(delivery=delivery),
            "timestamp": _AT,
            "run_id": "run-a",
            "sequence": 1,
        }
    )
    result = map_event(MappingState("run-a"), observation)
    intents = [r.body.value for r in result.records if r.body and r.body.field == "intent"]
    assert len(intents) == 1
    (intent,) = intents
    assert intent.control_id == control
    assert intent.step == step
    assert (intent.reason_code or None) == code
    if step == p.IntentStep.REFUSED:
        assert intent.reason
