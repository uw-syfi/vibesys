"""The shared correction for an invalid structured agent reply."""

from __future__ import annotations

import asyncio
from collections import deque

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vibesys.orchestration.structured_turn import structured_turn
from vs_runtime.api import StructuredResponseError


class _Reply(BaseModel):
    value: int


class _Session:
    """Answers each turn from a script; an exception entry fails that turn."""

    def __init__(self, *replies: _Reply | StructuredResponseError) -> None:
        self.replies = deque(replies)
        self.messages: list[str] = []

    async def turn(self, message: str, *, response: type[_Reply]) -> _Reply:
        del response
        self.messages.append(message)
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _run(session: _Session) -> _Reply:
    return asyncio.run(structured_turn(session, "work", _Reply))  # type: ignore[arg-type]  # a minimal session double: structured_turn only calls turn()


def test_a_valid_reply_needs_no_correction() -> None:
    session = _Session(_Reply(value=1))

    assert _run(session) == _Reply(value=1)
    assert session.messages == ["work"]


@given(detail=st.text(min_size=1).filter(str.strip))
def test_an_invalid_reply_is_corrected_with_its_validation_errors(detail: str) -> None:
    session = _Session(StructuredResponseError("r", _Reply, detail=detail), _Reply(value=2))

    assert _run(session) == _Reply(value=2)
    assert len(session.messages) == 2
    assert detail in session.messages[1]
    assert "_Reply" in session.messages[1]


def test_an_invalid_correction_raises_the_typed_error() -> None:
    second = StructuredResponseError("r", _Reply, detail="still bad")
    session = _Session(StructuredResponseError("r", _Reply), second)

    with pytest.raises(StructuredResponseError) as raised:
        _run(session)

    assert raised.value is second
    assert len(session.messages) == 2
