"""A recorded event's payload rejects keys it does not declare, except the fields it retired.

Covers every payload kind of the server's ``RunEvent`` and the core's ``CoreEvent``, read
back the way their stores read a recorded line.
"""

from __future__ import annotations

import json
import math
import types
import typing
from datetime import UTC, datetime

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

from server.events import ChatThreadId, EventData, EventType, RunEvent
from server.events import InputMeasurementFailedData as ServerInputFailed
from vibesys.api import (
    CoreEvent,
    CoreEventType,
    SteerDelivery,
    SteerDeliveryState,
    SteerMode,
    WireSpan,
)
from vibesys.events import (
    CoreEventData,
    EventPayload,
    InputMeasurementFailureKind,
    RunFailure,
    RunFailureKind,
)
from vibesys.events import InputMeasurementFailedData as CoreInputFailed
from vs_sim.api.testing import examples


def _deliveries() -> st.SearchStrategy[SteerDelivery]:
    """Steer steps with exactly the facts each state names (a step with any other is invalid)."""
    text = st.text(min_size=1, max_size=8)
    facts: dict[SteerDeliveryState, dict[str, st.SearchStrategy[object]]] = {
        SteerDeliveryState.QUEUED: {"mode": st.sampled_from(SteerMode)},
        SteerDeliveryState.WITHDRAW_QUEUED: {"withdraws": text},
        SteerDeliveryState.REACHED: {"role": text},
        SteerDeliveryState.REACHED_MID_TURN: {"role": text},
        SteerDeliveryState.IN_FORCE: {"role": text},
        SteerDeliveryState.DROPPED: {"reason": text},
        SteerDeliveryState.ENDED: {"reason": text},
        SteerDeliveryState.REFUSED: {"reason": text},
    }
    return st.sampled_from(SteerDeliveryState).flatmap(
        lambda state: st.builds(
            SteerDelivery,
            state=st.just(state),
            control_id=st.none() | text,
            **facts[state],
        )
    )


_JSON_SCALARS = st.none() | st.booleans() | st.integers() | st.text(max_size=8)
# Thread ids are drawn from the published schema of the id type itself, so they follow every
# rule it states (no trailing dot, no reserved device name), not a copy of some of them.
_THREAD_ID = TypeAdapter(ChatThreadId).json_schema()
_FIELD_VALUES: dict[str, st.SearchStrategy[object]] = {
    "thread_id": st.from_regex(_THREAD_ID["pattern"], fullmatch=True).filter(
        lambda value: len(value) <= _THREAD_ID["maxLength"]
    ),
    "value": st.floats(allow_nan=False, allow_infinity=False),
    "delivery": _deliveries(),
    # Bounded small, so a draw stays cheap; its own bound (``ge=0``) is kept for any other field.
    "rounds_finished": st.integers(min_value=0, max_value=99),
    # A span's id has a pattern a type alias carries, which ``st.from_type`` cannot see.
    "span": st.just(
        TypeAdapter(WireSpan).validate_json(
            '{"id": "flow:#1", "parent": null, "type": "flow", "name": "root",'
            ' "status": "running", "started_at": 0.0}'
        )
    ),
}
# Values by a field's type, for types Hypothesis cannot derive. Keyed by type, not by field
# name, so a new field that reuses a name (``failure`` of an input measurement is an enum) is
# still drawn from its own type.
_TYPE_VALUES: dict[object, st.SearchStrategy[object]] = {
    # Its optional constrained ``adopted_candidate`` trips its length filter on ``None``.
    RunFailure: st.builds(
        RunFailure,
        kind=st.sampled_from(RunFailureKind),
        reason=st.text(max_size=8),
        workstreams_started=st.integers(min_value=0, max_value=9),
        workstream_budget=st.integers(min_value=0, max_value=9),
        candidates_kept=st.integers(min_value=0, max_value=9),
        adopted_candidate=st.none(),
    ),
}
_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _kinds(union: object) -> tuple[type[EventPayload], ...]:
    return typing.get_args(typing.get_args(union)[0])


def _values(annotation: object, metadata: list[object]) -> st.SearchStrategy[object]:
    """Values of a field typed ``annotation`` that keep its bounds (``metadata``).

    A nested model is drawn like a payload, its own bounds kept, since ``st.from_type`` builds
    a model from its signature and drops them.
    """
    if annotation in _TYPE_VALUES:
        return _TYPE_VALUES[annotation]
    if annotation is typing.Any:
        return _JSON_SCALARS
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _models(annotation)
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        return st.one_of(*(_values(arg, metadata) for arg in typing.get_args(annotation)))
    # ``Annotated`` keeps the field's bounds, which ``st.from_type`` reads; built at runtime, it
    # is no static type, hence the cast.
    bounded = typing.Annotated[annotation, *metadata] if metadata else annotation
    drawn = st.from_type(typing.cast("type[object]", bounded))
    if annotation is float:  # a payload's numbers are finite: JSON has no NaN or infinity
        return drawn.filter(lambda value: math.isfinite(typing.cast("float", value)))
    return drawn


def _models[M: BaseModel](model: type[M]) -> st.SearchStrategy[M]:
    """Instances of ``model`` with every required field generated and the rest defaulted."""
    required = {
        name: _FIELD_VALUES.get(name) or _values(field.annotation, field.metadata)
        for name, field in model.model_fields.items()
        if field.is_required()
    }
    return st.fixed_dictionaries(required).map(model.model_validate)


def _payloads(model: type[EventPayload]) -> st.SearchStrategy[EventPayload]:
    """Payloads of ``model`` with every required field generated and the rest defaulted."""
    return _models(model)


def _record(envelope: type[RunEvent | CoreEvent], data: dict[str, object]) -> str:
    """One recorded event line carrying ``data`` as its payload."""
    event_type = EventType.RUN_STARTED if envelope is RunEvent else CoreEventType.RUN_STARTED
    event = envelope.model_validate({"timestamp": _AT, "type": event_type})
    return json.dumps({**event.model_dump(mode="json"), "data": data})


_CASES = [
    *((RunEvent, kind) for kind in _kinds(EventData)),
    *((CoreEvent, kind) for kind in _kinds(CoreEventData)),
]
_RETIRING = [case for case in _CASES if case[1].retired_keys]
_INPUT_FAILED = [case for case in _CASES if case[1] in {CoreInputFailed, ServerInputFailed}]


@settings(max_examples=examples(pr=20, nightly=200))
@given(case=st.sampled_from(_INPUT_FAILED), data=st.data())
def test_a_failed_input_measurement_payload_is_drawn_and_read_back(
    case: tuple[type[RunEvent | CoreEvent], type[EventPayload]], data: st.DataObject
) -> None:
    """The event a failed input measurement carries is among the cases, its ``failure`` an enum."""
    envelope, kind = case
    payload = data.draw(_payloads(kind))

    recorded = payload.model_dump(mode="json")

    assert envelope.model_validate_json(_record(envelope, recorded)).data == payload
    assert recorded["failure"] in {member.value for member in InputMeasurementFailureKind}


@settings(max_examples=examples(pr=60, nightly=2000))
@given(case=st.sampled_from(_CASES), data=st.data(), key=st.text(min_size=1, max_size=12))
def test_an_unknown_payload_key_is_rejected_by_name(
    case: tuple[type[RunEvent | CoreEvent], type[EventPayload]], data: st.DataObject, key: str
) -> None:
    envelope, kind = case
    payload = data.draw(_payloads(kind))
    assume(key not in kind.model_fields and key not in kind.retired_keys)
    recorded = payload.model_dump(mode="json")
    assert envelope.model_validate_json(_record(envelope, recorded)).data == payload

    with pytest.raises(ValidationError) as rejected:
        envelope.model_validate_json(_record(envelope, {**recorded, key: data.draw(_JSON_SCALARS)}))

    assert [(error["type"], error["loc"][-1]) for error in rejected.value.errors()] == [
        ("extra_forbidden", key)
    ]


@settings(max_examples=examples(pr=20, nightly=500))
@given(case=st.sampled_from(_RETIRING), data=st.data(), value=_JSON_SCALARS)
def test_a_retired_payload_key_replays_and_is_never_written_again(
    case: tuple[type[RunEvent | CoreEvent], type[EventPayload]], data: st.DataObject, value: object
) -> None:
    envelope, kind = case
    payload = data.draw(_payloads(kind))
    key = data.draw(st.sampled_from(sorted(kind.retired_keys)))
    recorded = {**payload.model_dump(mode="json"), key: value}

    replayed = envelope.model_validate_json(_record(envelope, recorded))

    assert replayed.data == payload
    assert key not in replayed.model_dump(mode="json")["data"]
