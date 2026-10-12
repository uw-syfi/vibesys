"""A reply is either the role's result or a wait for evaluations naming unique handles."""

from __future__ import annotations

from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

from vs_core.api import (
    AttemptId,
    DecisionFeedback,
    EventId,
    InvocationId,
    InvocationRef,
    Observation,
    ObservationStatus,
    RequestId,
    Scope,
    SessionId,
    SuspendedTurn,
    TurnResult,
    Waitable,
    WaitableReply,
    WaitingForEvaluation,
    turn_reply,
)


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


def _turn(output_json: str, *, suspending: bool) -> TurnResult:
    return TurnResult(
        invocation=InvocationRef(
            session_id=SessionId(root="s"), invocation_id=InvocationId(root="i"), generation=0
        ),
        observation=Observation(
            event_id=EventId(root="e"),
            request_id=RequestId(root="r"),
            scope=Scope(owner=AttemptId(root="a"), generation=0),
            sequence=1,
            observed_at=1.0,
            status=ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
        ),
        output_json=output_json,
        suspending=suspending,
    )


def _decode(payload: str) -> Verdict | WaitingForEvaluation | str:
    try:
        return _ENVELOPE.model_validate_json(payload).reply
    except ValidationError as error:
        return str(error)


@given(payload=st.one_of(st.text(), st.booleans().map(lambda p: f'{{"passed": {str(p).lower()}}}')))
def test_a_suspending_turn_is_a_wait_whatever_its_reply_says(payload: str) -> None:
    assert turn_reply(_turn(payload, suspending=True), _decode) == SuspendedTurn()


@given(payload=st.one_of(st.text(), st.booleans().map(lambda p: f'{{"passed": {str(p).lower()}}}')))
def test_a_turn_that_is_not_suspending_reads_its_reply(payload: str) -> None:
    assert turn_reply(_turn(payload, suspending=False), _decode) == _decode(payload)
