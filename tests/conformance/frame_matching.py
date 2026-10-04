"""Match a shared conformance scenario step against a received frame.

A step's ``expect`` is a partial protocol message (see ``FORMAT.md``): it names
``type`` plus only the fields the scenario constrains, and a runner matches it
as a subset of what the transport delivered. This module owns that match for
the Python runner, including the one frame the corpus cannot name by
discriminant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from server.api.protocol import Response
from server.api.schema import ProtocolDocument

if TYPE_CHECKING:
    from collections.abc import Mapping

_TYPE_KEY = "type"


def _response_section() -> str:
    """Return the document section that publishes the response envelope.

    ``Response`` is deliberately outside the ``type``-discriminated
    ``ServerMessage`` union: a control-path reply is correlated by
    ``request_id``, not by a tag. Scenarios therefore name it with a
    pseudo-type, and that name is derived here rather than written down, so the
    runner does not keep its own copy of a fact the protocol already fixes.
    ``ProtocolDocument`` is what the committed
    ``clients/backend-client/src/generated/protocol.schema.json`` is generated
    from (parity is pinned by ``tests/server/test_protocol_schema.py``), and
    that schema is the same artifact the Node corpus gate reads every real
    frame type out of, so its section name for ``Response`` is the one name
    both readers can agree on.

    Raises:
        ValueError: if the document does not publish ``Response`` exactly once,
            which would make the derived name ambiguous.
    """
    sections = [
        name
        for name, field in ProtocolDocument.model_fields.items()
        if field.annotation is Response
    ]
    if len(sections) != 1:
        message = f"ProtocolDocument must publish Response exactly once, found {sections}"
        raise ValueError(message)
    return sections[0]


RESPONSE_PSEUDO_TYPE = _response_section()


def _validation_error(frame: Mapping[str, Any]) -> str | None:
    """Return why ``frame`` is not a ``Response``, or ``None`` when it is one."""
    try:
        Response.model_validate(dict(frame))
    except ValidationError as error:
        return str(error)
    return None


def assert_frame_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Assert ``actual`` carries every key of ``expected`` with the same value.

    ``type`` is the only key handled specially. When a step expects
    ``RESPONSE_PSEUDO_TYPE`` the received frame carries no discriminant to
    compare, so the type claim is checked by validating the frame as a
    ``Response`` instead. That is strictly stronger than comparing a string:
    ``Response`` forbids extra fields, so any member of the discriminated
    server-message union fails it, and so does any frame missing the response
    envelope's required fields.

    Every other expected key is compared literally, the response payload
    included, so honoring the pseudo-type cannot turn a step into a vacuous
    assertion.

    Raises:
        AssertionError: naming the frame that is not a response, or the first
            expected key whose value differs.
    """
    if expected.get(_TYPE_KEY) == RESPONSE_PSEUDO_TYPE:
        reason = _validation_error(actual)
        assert reason is None, (
            f"expected a {RESPONSE_PSEUDO_TYPE} frame, received {actual!r}: {reason}"
        )
        expected = {key: value for key, value in expected.items() if key != _TYPE_KEY}
    for key, value in expected.items():
        assert actual.get(key) == value, f"expected {key}={value!r}, received {actual.get(key)!r}"
