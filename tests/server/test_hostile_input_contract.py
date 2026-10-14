"""One hostile-input contract for every decoder of bytes a server reads from a peer.

A decoder of untrusted input is total: whatever bytes arrive, it returns its
value or its own typed rejection and raises nothing else. Each decoder below
names the rejection it may raise, and every decoder meets the same corpus:
nesting deeper than any parser's recursion limit, a line past the transport
bound, unknown keys, frames that are not objects, invalid UTF-8, and a frame
cut short at every byte (a peer hanging up mid-frame). Python's ``json``
raised ``RecursionError`` on deep nesting and ``UnicodeDecodeError`` on bad
bytes, and each one escaped a handler that caught only decoding errors.

The transports carry these decoders over ``SimNetwork`` in
``test_unix_transport_faults.py``; this suite pins each decoder's own promise,
so a new decoder joins by adding one row.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from server.api.protocol import Response, SubscribeRequest, decode_request
from server.control_envelope import ResponseEnvelopeReader
from server.host_api import (
    HelperError,
    LiveInstanceRecord,
    UnreadableRecord,
    parse_helper_message,
    parse_helper_request,
    parse_record,
)
from server.host_helper import ChannelState, Received, Send, step
from server.transport.unix_jsonl import MAX_REQUEST_LINE_BYTES
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

_DEEP = 100_000
"""Far past CPython's recursion limit, and past pydantic's nesting bound."""

_VALID = (
    SubscribeRequest(after_sequence=0).model_dump_json().encode(),
    json.dumps(
        {"type": "hello", "request_id": "h", "client_versions": [1, 1], "app_version": "v"}
    ).encode(),
    json.dumps({"version": 1, "id": "i", "pid": 1}).encode(),
)
"""Well-formed frames whose every prefix is a peer hanging up mid-frame."""

_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner),
    max_leaves=8,
)


def _hostile_frames() -> st.SearchStrategy[bytes]:
    return st.one_of(
        st.sampled_from([b"[", b'{"a":', b'{"type":"command.stop","x":[']).map(
            lambda opener: opener * _DEEP
        ),
        st.just(b'{"type":"' + b"x" * (MAX_REQUEST_LINE_BYTES + 1) + b'"}'),
        st.sampled_from([b"null", b"1", b'"s"', b"[]", b"true", b"1e999", b"NaN", b""]),
        st.binary(max_size=32).map(lambda prefix: prefix + b"\xff\xfe"),
        st.sampled_from(_VALID).flatmap(
            lambda frame: st.integers(0, len(frame)).map(lambda cut: frame[:cut])
        ),
        st.dictionaries(st.text(max_size=8), _JSON_VALUES, max_size=4).map(
            lambda value: json.dumps(value).encode()
        ),
        st.binary(max_size=128),
    )


@dataclass(frozen=True)
class _Decoder:
    """One decoder of peer bytes, and the exceptions it may answer with."""

    decode: Callable[[bytes], object]
    rejections: tuple[type[Exception], ...]
    """Empty for a decoder that answers every frame with a value."""


def _decode_request(frame: bytes) -> object:
    result = decode_request(frame)
    assert isinstance(result, Response) or result.type
    return result


def _parse_record(frame: bytes) -> object:
    result = parse_record("0123456789ab", frame)
    assert isinstance(result, LiveInstanceRecord | UnreadableRecord)
    return result


def _helper_channel(frame: bytes) -> object:
    """A helper channel's first line: a request, or a refusal it sends back."""
    _, outputs = step(ChannelState(), Received(frame, 0.0), "v")
    assert (
        all(
            isinstance(output, Send) and isinstance(output.message, HelperError)
            for output in outputs
        )
        or frame in _VALID
    )
    return outputs


_DECODERS = {
    "protocol-request": _Decoder(_decode_request, ()),
    "instance-record": _Decoder(_parse_record, ()),
    "helper-channel": _Decoder(_helper_channel, ()),
    "helper-request": _Decoder(parse_helper_request, (ValidationError,)),
    "helper-message": _Decoder(parse_helper_message, (ValidationError,)),
    "response-envelope": _Decoder(ResponseEnvelopeReader.model_validate_json, (ValidationError,)),
}


@pytest.mark.parametrize("decoder", list(_DECODERS.values()), ids=list(_DECODERS))
@settings(deadline=None, max_examples=examples(pr=20, nightly=500))
@given(frame=_hostile_frames())
@example(frame=b"[" * _DEEP)
@example(frame=b'"\xff"')
def test_a_decoder_answers_any_hostile_frame_with_its_value_or_its_own_rejection(
    decoder: _Decoder, frame: bytes
) -> None:
    with suppress(*decoder.rejections):
        decoder.decode(frame)
