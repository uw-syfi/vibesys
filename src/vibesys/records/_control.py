"""Boundary-applied controls and steer delivery links."""

import vibesys.events as e
import vs_proto.api as p
from vibesys.records._agents import owner
from vibesys.records._common import Projection, enum_value


def lifecycle(projection: Projection) -> None:
    event = projection.event
    states = {
        e.CoreEventType.PAUSE_REQUESTED: p.Lifecycle(
            state=p.Oneof(
                "running", p.Running(hold=p.Hold(reason=p.Oneof("operator", p.OperatorHold())))
            )
        ),
        e.CoreEventType.PAUSED: p.Lifecycle(
            state=p.Oneof(
                "running", p.Running(hold=p.Hold(reason=p.Oneof("operator", p.OperatorHold())))
            )
        ),
        e.CoreEventType.RESUMED: p.Lifecycle(state=p.Oneof("running", p.Running())),
        e.CoreEventType.STOP_REQUESTED: p.Lifecycle(
            state=p.Oneof("draining", p.Draining(goal=p.DrainGoal.STOP))
        ),
        e.CoreEventType.STOPPED: p.Lifecycle(
            state=p.Oneof("ended", p.Ended(outcome=p.Outcome.STOPPED))
        ),
    }
    projection.emit("lifecycle", states[event.type])


def steer(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.SteerDeliveryData):
        message = f"{projection.event.type}: requires SteerDeliveryData"
        raise TypeError(message)
    delivery = data.delivery
    control_id = delivery.control_id or f"journal:{projection.event.sequence}"
    if delivery.state.value in {"reached", "reached_mid_turn", "in_force"}:
        span = projection.spans.get(owner(projection))
        if span is not None and span.label is not None:
            link = p.SpanLink(rel="steer", to=control_id)
            if link not in span.label.links:
                span.label.links.append(link)
            projection.span(span)
    refused = delivery.state.value in {"dropped", "refused"}
    intent = p.IntentState(
        control_id=control_id,
        action=enum_value(p.IntentAction, delivery.action),
        step=p.IntentStep.REFUSED if refused else p.IntentStep.APPLIED,
        reason=delivery.reason if refused else None,
        mode=None if delivery.mode is None else enum_value(p.SteerMode, delivery.mode),
        withdraws=delivery.withdraws,
    )
    projection.emit("intent", intent)


MAPPERS = dict.fromkeys(
    (
        e.CoreEventType.PAUSE_REQUESTED,
        e.CoreEventType.PAUSED,
        e.CoreEventType.RESUMED,
        e.CoreEventType.STOP_REQUESTED,
        e.CoreEventType.STOPPED,
    ),
    lifecycle,
)
MAPPERS.update(
    dict.fromkeys(
        (
            e.CoreEventType.STEER_QUEUED,
            e.CoreEventType.GUIDANCE_QUEUED,
            e.CoreEventType.WITHDRAW_QUEUED,
            e.CoreEventType.GUIDANCE_ENDED,
            e.CoreEventType.GUIDANCE_REACHED,
            e.CoreEventType.STEER_CONSUMED,
            e.CoreEventType.STEER_DELIVERED_MID_TURN,
            e.CoreEventType.STEER_DROPPED,
        ),
        steer,
    )
)
