"""The shared correction for an invalid structured agent reply."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vibesys.orchestration.single import PLUGIN
from vibesys.orchestration.single.agents import IMPLEMENTER
from vibesys.orchestration.structured_turn import structured_turn
from vs_agent.api.testing import FakeAgentInvocationStore
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    SessionClosedError,
    StructuredResponseError,
    bind_agent_invocation,
)
from vs_runtime.api.testing import FakeRun, FakeWorkspace, FakeWorkspaceAgentSessions


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


def test_journaled_invalid_reply_and_accepted_correction_replay_after_restart() -> None:
    calls: list[str] = []
    store = FakeAgentInvocationStore()
    role = AgentRole(id="worker", system_prompt="Work.")

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> object:
        calls.append(message)
        return {"value": "invalid"} if len(calls) == 1 else {"value": 7}

    async def scenario() -> None:
        for _ in range(2):
            owner = FakeWorkspaceAgentSessions(
                (role,),
                responder=respond,
                supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
            )
            owner.bind_invocation_store(store)
            session = await owner.create_session(
                role, workspace=FakeWorkspace(), member_id="member"
            )
            try:
                assert await structured_turn(
                    session, "work", _Reply, invocation_id="initial"
                ) == _Reply(value=7)
            finally:
                await owner.close()
        assert len(calls) == 2

    asyncio.run(scenario())


def test_interrupt_during_journaled_correction_releases_replacement_turn() -> None:
    calls: list[str] = []
    role = AgentRole(id="worker", system_prompt="Work.")

    async def scenario() -> None:
        correction_started = asyncio.Event()
        correction_release = asyncio.Event()

        async def respond(
            _role: AgentRole,
            _history: tuple[str, ...],
            message: str,
            _response: type[BaseModel] | None,
        ) -> object:
            calls.append(message)
            if len(calls) == 1:
                return {"value": "invalid"}
            if len(calls) == 2:
                correction_started.set()
                await correction_release.wait()
            return {"value": 7}

        owner = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        turn = asyncio.create_task(
            structured_turn(session, "work", _Reply, invocation_id="initial")
        )
        await correction_started.wait()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        session.release_interrupted("initial")
        assert await structured_turn(
            session, "replacement", _Reply, invocation_id="next"
        ) == _Reply(value=7)
        await owner.close()
        assert len(calls) == 3

    asyncio.run(scenario())


def test_invocation_binding_preserves_closed_session_fence() -> None:
    """Recorded completion does not reopen a conversation after cleanup."""
    role = AgentRole(id="worker", system_prompt="Work.")
    script = _Script(_Reply(value=7))

    async def scenario() -> None:
        owner = FakeWorkspaceAgentSessions(
            (role,),
            responder=script.respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        bound = bind_agent_invocation(session, "initial")
        assert await bound.turn("work", response=_Reply) == _Reply(value=7)
        await bound.close()
        assert bound.closed
        with pytest.raises(SessionClosedError):
            await bound.turn("work", response=_Reply)
        assert script.messages == ["work"]
        await owner.close()

    asyncio.run(scenario())
