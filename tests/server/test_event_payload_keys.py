"""A recorded event's payload rejects keys it does not declare, except the fields it retired.

Covers every payload kind of the server's ``RunEvent`` and the core's ``CoreEvent``, read
back the way their stores read a recorded line.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError
from tests.support.event_models import JSON_SCALARS as _JSON_SCALARS
from tests.support.event_models import payload_models as _kinds
from tests.support.event_models import payloads as _payloads

from server.events import EventData, EventType, RunEvent
from server.events import InputMeasurementFailedData as ServerInputFailed
from server.events import WinnerSelectedData as WireWinnerSelected
from vibesys.api import (
    CheckoutStatus,
    CoreEvent,
    CoreEventType,
    WinnerSelectedData,
)
from vibesys.events import (
    CoreEventData,
    EventPayload,
    InputMeasurementFailureKind,
)
from vibesys.events import InputMeasurementFailedData as CoreInputFailed
from vs_sim.api.testing import examples

_AT = datetime(2026, 1, 1, tzinfo=UTC)


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


@pytest.mark.parametrize(
    "case", _CASES, ids=lambda case: f"{case[0].__name__}-{case[1].model_fields['kind'].default}"
)
@settings(max_examples=examples(pr=40, nightly=200))
@given(data=st.data())
def test_every_payload_kind_is_drawn_valid(
    case: tuple[type[RunEvent | CoreEvent], type[EventPayload]], data: st.DataObject
) -> None:
    """The generator draws each kind within its own rules (field constraints, finite numbers).

    A kind it cannot draw would fail the properties above only on the nights that happen to draw
    its invalid corner, so each kind is drawn here at the pull-request depth too.
    """
    _, kind = case
    payload = data.draw(_payloads(kind))
    assert kind.model_validate(payload.model_dump(mode="python")) == payload


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


def test_a_core_event_payload_declares_no_field_the_wire_rejects() -> None:
    """A core payload is revalidated as the wire payload, so every field must be declared there.

    ``project_event`` dumps the core payload, including an explicit None, and the wire model
    forbids keys it does not declare.
    """
    wire = {model.model_fields["kind"].default: model for model in _kinds(EventData)}
    missing = {
        model.model_fields["kind"].default: sorted(
            set(model.model_fields) - set(wire[model.model_fields["kind"].default].model_fields)
        )
        for model in _kinds(CoreEventData)
        if model.model_fields["kind"].default in wire
    }
    assert {kind: fields for kind, fields in missing.items() if fields} == {}


@pytest.mark.regression
@settings(max_examples=examples(pr=8, nightly=40))
@given(status=st.none() | st.sampled_from(tuple(CheckoutStatus)))
@example(status=None)  # the reported case: null is dumped, and the wire model rejected the key
def test_a_winner_selection_checkout_status_crosses_onto_the_wire(
    status: CheckoutStatus | None,
) -> None:
    """None is recorded as null, and every checkout status is a wire value."""
    payload = WinnerSelectedData(selection="baseline", revision="input", checkout_status=status)
    wire = TypeAdapter(EventData).validate_python(payload.model_dump(mode="python"))

    assert isinstance(wire, WireWinnerSelected)
    assert wire.checkout_status == status
