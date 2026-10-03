"""The shared correction for an invalid structured agent reply."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vibesys.orchestration.single import PLUGIN
from vibesys.orchestration.single.agents import IMPLEMENTER
from vibesys.orchestration.structured_turn import structured_turn
from vs_runtime.api import StructuredResponseError
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole


class _Reply(BaseModel):
    value: int


class _Script:
    """Answers each turn from a list; an exception entry fails that turn."""

    def __init__(self, *replies: _Reply | StructuredResponseError) -> None:
        self.replies = deque(replies)
        self.messages: list[str] = []

    def respond(
        self,
        _role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.messages.append(message)
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _run(script: _Script) -> _Reply:
    async def scenario() -> _Reply:
        run = FakeRun(PLUGIN, project_root=Path("/candidate"), responder=script.respond)
        try:
            session = await run.agents.create_session(IMPLEMENTER, workspace=run.workspaces.root)
            return await structured_turn(session, "work", _Reply)
        finally:
            await run.close()

    return asyncio.run(scenario())


def test_a_valid_reply_needs_no_correction() -> None:
    script = _Script(_Reply(value=1))

    assert _run(script) == _Reply(value=1)
    assert script.messages == ["work"]


@given(detail=st.text(min_size=1).filter(str.strip))
def test_an_invalid_reply_is_corrected_with_its_validation_errors(detail: str) -> None:
    script = _Script(StructuredResponseError("r", _Reply, detail=detail), _Reply(value=2))

    assert _run(script) == _Reply(value=2)
    assert len(script.messages) == 2
    assert detail in script.messages[1]
    assert "_Reply" in script.messages[1]


def test_an_invalid_correction_raises_the_typed_error() -> None:
    second = StructuredResponseError("r", _Reply, detail="still bad")
    script = _Script(StructuredResponseError("r", _Reply), second)

    with pytest.raises(StructuredResponseError) as raised:
        _run(script)

    assert raised.value is second
    assert len(script.messages) == 2
