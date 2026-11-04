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
        e.CoreEventType.PARK_REQUESTED: p.Lifecycle(
            state=p.Oneof("draining", p.Draining(goal=p.DrainGoal.PARK))
        ),
        e.CoreEventType.PARKED: p.Lifecycle(state=p.Oneof("parked", p.Parked())),
        e.CoreEventType.STOP_REQUESTED: p.Lifecycle(
            state=p.Oneof("draining", p.Draining(goal=p.DrainGoal.STOP))
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
    if projection.state.native_intents:
        return
    refused = delivery.state.value in {"dropped", "refused"}
    intent = p.IntentState(
        control_id=control_id,
        action=enum_value(p.IntentAction, delivery.action),
        step=p.IntentStep.REFUSED if refused else p.IntentStep.APPLIED,
        reason=delivery.reason if refused else None,
        # Dropped and ended are what the card tells apart from a refusal and from a withdrawal.
        reason_code=delivery.state.value if delivery.state.value in {"dropped", "ended"} else None,
        mode=None if delivery.mode is None else enum_value(p.SteerMode, delivery.mode),
        withdraws=delivery.withdraws,
        target=None
        if delivery.target is None
        else p.SteerTarget(
            kind=enum_value(p.SteerTargetKind, delivery.target.kind), id=delivery.target.id
        ),
    )
    prior = projection.intents.get(control_id)
    if prior is not None:
        previous = p.IntentState.from_binary(prior)
        for name in ("mode", "target", "withdraws"):
            if not intent.has_field(name) and previous.has_field(name):
                field = next(field for field in intent.desc().fields if field.name == name)
                intent[field] = previous[field]
    projection.intents[control_id] = intent.to_binary()
    projection.emit("intent", intent)


def refusal(projection: Projection) -> None:
    if isinstance(projection.event.data, e.SteerDeliveryData):
        steer(projection)
    else:
        projection.emit(
            "notice",
            p.Notice(
                severity=p.Severity.WARNING,
                code=p.NoticeCode.FRAMEWORK_WARNING,
                message=projection.text(
                    "run", "control_refused", projection.event.text or projection.event.type.value
                ),
            ),
        )


def outcome(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.IntentStateData):
        message = "intent_state requires IntentStateData"
        raise TypeError(message)
    intent = p.IntentState.from_json(data.state_json)
    p.validate(intent)
    if intent.control_id in projection.intents:
        return
    projection.intents[intent.control_id] = intent.to_binary()
    projection.emit("intent", intent)


MAPPERS = {
    e.CoreEventType.CONTROL_REFUSED: refusal,
    e.CoreEventType.REQUEST_REJECTED: refusal,
    e.CoreEventType.INTENT_STATE: outcome,
}
MAPPERS.update(
    dict.fromkeys(
        (
            e.CoreEventType.PAUSE_REQUESTED,
            e.CoreEventType.PAUSED,
            e.CoreEventType.RESUMED,
            e.CoreEventType.PARK_REQUESTED,
            e.CoreEventType.PARKED,
            e.CoreEventType.STOP_REQUESTED,
        ),
        lifecycle,
    )
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
