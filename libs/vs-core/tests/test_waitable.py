"""A reply is either the role's result or a wait for evaluations naming unique handles."""

from __future__ import annotations

from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

from vs_core.api import DecisionFeedback, Waitable, WaitableReply, WaitingForEvaluation


class Verdict(BaseModel):
    passed: bool


class NestedVerdict(BaseModel):
    feedback: DecisionFeedback


class ReplyVerdict(BaseModel):
    reply: str


_REPLY = TypeAdapter(Waitable[Verdict])
_ENVELOPE = WaitableReply[Verdict]
_HANDLES = st.lists(st.text("abc123", min_size=1, max_size=4), min_size=1, max_size=4, unique=True)


@given(handles=_HANDLES)
def test_a_wait_parses_as_a_wait(handles: list[str]) -> None:
    reply = _REPLY.validate_python({"kind": "waiting_for_evaluation", "handles": handles})
    assert reply == WaitingForEvaluation(kind="waiting_for_evaluation", handles=tuple(handles))


@given(passed=st.booleans())
def test_a_result_without_a_kind_parses_as_the_result(*, passed: bool) -> None:
    assert _REPLY.validate_json(f'{{"passed": {str(passed).lower()}}}') == Verdict(passed=passed)


@given(passed=st.booleans())
def test_an_enveloped_result_parses_and_serializes_at_an_object_root(*, passed: bool) -> None:
    reply = _ENVELOPE.model_validate({"reply": {"passed": passed}})

    assert reply.reply == Verdict(passed=passed)
    assert reply.model_dump(mode="json") == {"reply": {"passed": passed}}


@given(passed=st.booleans())
def test_a_legacy_bare_result_is_accepted_but_serializes_as_an_envelope(*, passed: bool) -> None:
    reply = _ENVELOPE.model_validate({"passed": passed})

    assert reply.reply == Verdict(passed=passed)
    assert reply.model_dump(mode="json") == {"reply": {"passed": passed}}


@pytest.mark.parametrize(
    "payload",
    [
        {"reply": "legacy"},
        MappingProxyType({"reply": "legacy"}),
    ],
    ids=["dict", "mapping"],
)
def test_a_legacy_result_may_itself_have_a_reply_field(payload: object) -> None:
    envelope = WaitableReply[ReplyVerdict].model_validate(payload)

    assert envelope.reply == ReplyVerdict(reply="legacy")
    assert envelope.model_dump(mode="json") == {"reply": {"reply": "legacy"}}


def test_the_envelope_schema_has_an_object_root_and_no_one_of() -> None:
    schema = WaitableReply[NestedVerdict].model_json_schema()

    assert schema["type"] == "object"
    assert "oneOf" not in str(schema)


@pytest.mark.parametrize("handles", [[], [" "], ["a", "a"], [" a"]])
def test_a_wait_names_nonblank_unique_handles(handles: list[str]) -> None:
    with pytest.raises(ValidationError):
        _REPLY.validate_python({"kind": "waiting_for_evaluation", "handles": handles})
