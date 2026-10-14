"""Every transport rejects a malformed request frame with the same typed response.

``decode_request`` is total: for any frame it returns a request or a failed
``Response``, and never raises, whatever the nesting depth or the bytes.
"""

from __future__ import annotations

import json
import sys

from hypothesis import example, given, settings
from hypothesis import strategies as st

from server.api.protocol import Response, decode_request
from vs_sim.api.testing import ManualClock, examples

_REJECTED = "Request received invalid input"
_SCALARS = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text()
_JSON_VALUES = st.recursive(
    _SCALARS,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(), children, max_size=3)
    ),
    max_leaves=8,
)
# Depths around the parser's limit and past Python's recursion limit; the
# explicit examples add 100 000.
_DEPTHS = st.integers(0, 3) | st.sampled_from([199, 200, 201, 1_000, sys.getrecursionlimit() + 1])
# A frame shaped like a request whose type is drawn from real and bogus kinds.
_TYPES = st.sampled_from(["query.snapshot", "command.pause", "subscribe", "no.such.type", ""])


def _nested(depth: int, inner: str, *, closed: bool, as_object: bool) -> str:
    """``inner`` wrapped in ``depth`` arrays or objects, closed or cut short.

    Built as text: ``json.dumps`` would itself recurse once per level.
    """
    opening, closing = ('{"k":', "}") if as_object else ("[", "]")
    return opening * depth + inner + (closing * depth if closed else "")


def _object_ids(frame: str | bytes) -> tuple[str, str] | None:
    """The ids a rejection must echo, or None when the frame is no JSON object."""
    try:
        value = json.loads(frame)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    client_id = value.get("client_id")
    return str(value.get("request_id", "unknown")), client_id if isinstance(client_id, str) else ""


def _check(frame: str | bytes) -> None:
    """A request is exactly what a well-formed object frame named; anything else is rejected."""
    decoded = decode_request(frame, clock=ManualClock())

    if isinstance(decoded, Response):
        assert decoded.ok is False
        assert decoded.diagnostic is not None
        assert decoded.diagnostic.summary == _REJECTED
        ids = _object_ids(frame)
        if ids is None:
            assert (decoded.request_id, decoded.client_id) == ("unknown", "")
        elif ids[0] != "unknown":
            assert (decoded.request_id, decoded.client_id) == ids
    else:
        value = json.loads(frame)
        assert isinstance(value, dict)
        assert value["type"] == decoded.type
        assert decode_request(decoded.model_dump_json(), clock=ManualClock()) == decoded


@settings(max_examples=examples(pr=25, nightly=1000))
@given(
    value=_JSON_VALUES,
    kind=_TYPES,
    request_id=st.none() | st.text(max_size=8),
    depth=_DEPTHS,
    closed=st.booleans(),
    as_object=st.booleans(),
)
@example(
    value=None, kind="query.snapshot", request_id="r", depth=100_000, closed=True, as_object=False
)
@example(
    value=None, kind="query.snapshot", request_id=None, depth=100_000, closed=False, as_object=True
)
@example(value=None, kind="query.snapshot", request_id="r", depth=0, closed=True, as_object=False)
def test_any_json_frame_decodes_to_its_request_or_a_typed_rejection(  # noqa: PLR0913  # lint-waiver: LW-302314 [PLR0913]; each argument is one independent axis Hypothesis draws, and bundling them into a composite strategy would hide which axis a falsifying example varied.
    *,
    value: object,
    kind: str,
    request_id: str | None,
    depth: int,
    closed: bool,
    as_object: bool,
) -> None:
    """Requests, near-requests, arbitrary values, and deep or unclosed nesting.

    - A frame that decodes is a JSON object whose ``type`` the request carries,
      and the request survives its own encoding.
    - Every other frame is a failed ``Response`` with the invalid-input
      diagnostic, echoing the frame's ids when it is an object that has them.
    """
    request: dict[str, object] = {"type": kind}
    if request_id is not None:
        request["request_id"] = request_id
    if value is not None:
        request["extra"] = value
    inner = json.dumps(request if depth == 0 or value is None else value)

    _check(_nested(depth, inner, closed=closed, as_object=as_object))


@settings(max_examples=examples(pr=25, nightly=1000))
@given(frame=st.binary(max_size=64) | st.text(max_size=64).map(str.encode))
@example(frame=b"[" * 100_000)
@example(frame=b"\xff\xfe{}")
def test_any_bytes_decode_to_a_request_or_a_typed_rejection(frame: bytes) -> None:
    _check(frame)


@settings(max_examples=examples(pr=20, nightly=300))
@given(request_id=st.text(min_size=1), client_id=st.text())
def test_a_rejection_echoes_the_ids_of_an_object_frame(request_id: str, client_id: str) -> None:
    frame = json.dumps({"request_id": request_id, "client_id": client_id, "type": "no.such.type"})

    decoded = decode_request(frame, clock=ManualClock())

    assert isinstance(decoded, Response)
    assert (decoded.request_id, decoded.client_id) == (request_id, client_id)
