"""Every transport rejects a malformed request frame with the same typed response."""

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from server.api.protocol import Response, SnapshotQuery, decode_request
from vs_sim.api.testing import examples

_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=8,
)


@settings(max_examples=examples(pr=20, nightly=300))
@given(value=_JSON_VALUES)
def test_any_json_value_decodes_to_a_request_or_a_typed_rejection(value: object) -> None:
    decoded = decode_request(json.dumps(value))

    if isinstance(decoded, Response):
        assert decoded.ok is False
        assert decoded.diagnostic is not None
        assert decoded.diagnostic.summary == "Request received invalid input"
        if not isinstance(value, dict):
            assert (decoded.request_id, decoded.client_id) == ("unknown", "")


@settings(max_examples=examples(pr=20, nightly=300))
@given(frame=st.binary())
def test_any_bytes_decode_to_a_request_or_a_typed_rejection(frame: bytes) -> None:
    decoded = decode_request(frame)

    assert isinstance(decoded, Response | SnapshotQuery) or decoded.request_id is not None
    if isinstance(decoded, Response):
        assert decoded.ok is False


@settings(max_examples=examples(pr=20, nightly=300))
@given(request_id=st.text(min_size=1), client_id=st.text())
def test_a_rejection_echoes_the_ids_of_an_object_frame(request_id: str, client_id: str) -> None:
    frame = json.dumps({"request_id": request_id, "client_id": client_id, "type": "no.such.type"})

    decoded = decode_request(frame)

    assert isinstance(decoded, Response)
    assert (decoded.request_id, decoded.client_id) == (request_id, client_id)
