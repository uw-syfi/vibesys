"""The corpus response pseudo-type: its one definition, and its limits."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.conformance.frame_matching import RESPONSE_PSEUDO_TYPE, assert_frame_matches

from server.api.protocol import (
    EventBatchMessage,
    ProtocolErrorMessage,
    Response,
    ServerMessage,
    SubscribedMessage,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

# The Node corpus gate runs in its own process, so it cannot import the
# derivation. It declares the same name as a literal and this test is what
# fails when the two drift apart.
_GATE = Path("clients/scripts/check_conformance_corpus.mjs")
_GATE_CONSTANT = re.compile(r"^const RESPONSE_PSEUDO_TYPE = '([^']*)';$", re.MULTILINE)

_IDENTIFIERS = st.text(alphabet="abc", max_size=3)


def _frame(message: BaseModel) -> dict[str, Any]:
    return dict(message.model_dump(mode="json"))


def test_the_pseudo_type_exists_because_a_response_carries_no_discriminant() -> None:
    """The premise of the whole relaxation, pinned so a model change surfaces here.

    If ``Response`` ever joined the ``type``-discriminated union, scenarios
    would name it by its tag and the pseudo-type would have no reason to exist.
    """
    assert "type" not in Response.model_fields


def test_the_node_corpus_gate_names_the_same_pseudo_type() -> None:
    match = _GATE_CONSTANT.search(_GATE.read_text())
    assert match is not None, f"{_GATE} no longer declares a RESPONSE_PSEUDO_TYPE literal"
    assert match.group(1) == RESPONSE_PSEUDO_TYPE


def test_a_response_frame_satisfies_the_pseudo_type() -> None:
    assert_frame_matches(_frame(Response(request_id="r-1")), {"type": RESPONSE_PSEUDO_TYPE})


@pytest.mark.parametrize(
    "message",
    [
        SubscribedMessage(request_id="r-1", run_id="run-1", latest_sequence=0),
        EventBatchMessage(events=[]),
        ProtocolErrorMessage(code="invalid_value", message="no"),
    ],
    ids=lambda message: str(message.type),
)
def test_a_discriminated_server_message_never_satisfies_the_pseudo_type(
    message: ServerMessage,
) -> None:
    """The relaxation must not swallow a real mismatch.

    ``subscribed`` is the concrete case: the two ``capability-probe-*``
    scenarios are answered by it today, and they must keep failing rather than
    quietly pass once the pseudo-type is honored.
    """
    with pytest.raises(AssertionError):
        assert_frame_matches(_frame(message), {"type": RESPONSE_PSEUDO_TYPE})


@given(
    client_id=_IDENTIFIERS,
    ok=st.booleans(),
    expected_client_id=_IDENTIFIERS,
    expected_ok=st.booleans(),
)
def test_a_response_step_still_compares_every_other_expected_key(
    *,
    client_id: str,
    ok: bool,
    expected_client_id: str,
    expected_ok: bool,
) -> None:
    """Honoring the pseudo-type relaxes ``type`` alone, never the payload.

    Expressed as a property so the guarantee covers every combination of
    agreeing and disagreeing constrained fields, not one hand-picked pair.
    """
    frame = _frame(Response(request_id="r-1", client_id=client_id, ok=ok))
    expected = {
        "type": RESPONSE_PSEUDO_TYPE,
        "client_id": expected_client_id,
        "ok": expected_ok,
    }
    if client_id == expected_client_id and ok == expected_ok:
        assert_frame_matches(frame, expected)
        return
    with pytest.raises(AssertionError):
        assert_frame_matches(frame, expected)


def test_a_mismatched_payload_names_the_offending_key() -> None:
    frame = _frame(Response(request_id="r-1", ok=True))
    with pytest.raises(AssertionError, match=r"expected ok=False, received True"):
        assert_frame_matches(frame, {"type": RESPONSE_PSEUDO_TYPE, "ok": False})


def test_a_frame_that_is_not_a_response_reports_why() -> None:
    with pytest.raises(AssertionError, match=r"expected a response frame, received"):
        assert_frame_matches({"ok": True}, {"type": RESPONSE_PSEUDO_TYPE})


@given(
    expected_type=st.sampled_from(["subscribed", "event_batch", "protocol_error"]),
    actual_type=st.sampled_from(["subscribed", "event_batch", "protocol_error"]),
)
def test_a_real_frame_type_is_still_matched_literally(
    expected_type: str,
    actual_type: str,
) -> None:
    """Every type that is not the pseudo-type keeps the plain subset semantics."""
    if expected_type == actual_type:
        assert_frame_matches({"type": actual_type}, {"type": expected_type})
        return
    with pytest.raises(AssertionError):
        assert_frame_matches({"type": actual_type}, {"type": expected_type})
