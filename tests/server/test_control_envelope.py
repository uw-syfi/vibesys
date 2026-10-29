"""A light client reads the envelope of any response the full protocol writes."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.support import REQUEST_TIME

from server.api.protocol import CommandAck, Response
from server.api.schema import fully_populated_response
from server.control_envelope import ResponseEnvelope, ResponseEnvelopeReader
from vibesys.api import ControlAction

_ENVELOPE_FIELDS = list(ResponseEnvelope.model_fields)


def test_the_full_response_extends_the_envelope_fields_in_order() -> None:
    assert list(Response.model_fields)[: len(_ENVELOPE_FIELDS)] == _ENVELOPE_FIELDS


@given(
    ok=st.booleans(),
    error=st.none() | st.text(),
    ack=st.none()
    | st.builds(
        CommandAck,
        action=st.sampled_from(ControlAction),
        status=st.sampled_from(["pending", "consumed"]),
    ),
)
def test_the_reader_recovers_the_envelope_of_a_payload_carrying_response(
    *, ok: bool, error: str | None, ack: CommandAck | None
) -> None:
    response = fully_populated_response().model_copy(update={"ok": ok, "error": error, "ack": ack})

    read = ResponseEnvelopeReader.model_validate_json(response.model_dump_json())

    assert read.model_dump(include=set(_ENVELOPE_FIELDS)) == response.model_dump(
        include=set(_ENVELOPE_FIELDS)
    )


def test_the_reader_knows_every_field_of_the_full_response() -> None:
    assert list(ResponseEnvelopeReader.model_fields) == list(Response.model_fields)


@given(
    populated=st.booleans(),
    key=st.text(min_size=1, max_size=12).filter(lambda key: key not in Response.model_fields),
    value=st.none() | st.booleans() | st.integers() | st.text(max_size=8),
)
def test_the_reader_rejects_a_key_the_full_response_does_not_have(
    *, populated: bool, key: str, value: object
) -> None:
    response = (
        fully_populated_response()
        if populated
        else Response(timestamp=REQUEST_TIME, request_id="r")
    )
    line = json.dumps({**json.loads(response.model_dump_json()), key: value})

    with pytest.raises(ValidationError) as rejected:
        ResponseEnvelopeReader.model_validate_json(line)

    assert [(error["type"], error["loc"]) for error in rejected.value.errors()] == [
        ("extra_forbidden", (key,))
    ]


def test_the_reader_still_validates_the_envelope_fields() -> None:
    line = (
        Response(timestamp=REQUEST_TIME, request_id="r")
        .model_dump_json()
        .replace('"ok":true', '"ok":"maybe"')
    )

    with pytest.raises(ValidationError, match="ok"):
        ResponseEnvelopeReader.model_validate_json(line)
