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
from vs_agent.api import (
    AgentInvocationState,
    AgentOutputSchemaError,
    Completed,
    InvalidResponse,
    InvocationConflictError,
    SessionPersistenceError,
    SessionResumeError,
    Unknown,
)
from vs_agent.api.testing import FakeAgentInvocationStore
from vs_prompts.api import TemplateRenderer
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


@pytest.mark.parametrize("initial_fault", ["invalid-dict", "malformed-text", "host-schema-error"])
def test_journaled_invalid_reply_and_accepted_correction_replay_after_restart(
    initial_fault: str,
) -> None:
    calls: list[str] = []
    store = FakeAgentInvocationStore()
    role = AgentRole(id="worker", system_prompt="Work.")

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> object:
        calls.append(message)
        if len(calls) > 1:
            return {"value": 7}
        if initial_fault == "host-schema-error":
            raise StructuredResponseError(role.id, _Reply, detail="missing value")
        return "not JSON" if initial_fault == "malformed-text" else {"value": "invalid"}

    async def scenario() -> None:
        checkpoints = []
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
                outcome = session.inspect("initial/correction")
                assert isinstance(outcome, Completed)
                assert _Reply.model_validate_json(outcome.result.text) == _Reply(value=7)
                checkpoints.append(outcome.checkpoint)
            finally:
                await owner.close()
        assert checkpoints[0] == checkpoints[1]
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


def test_native_fake_schema_failure_refuses_checkpointless_correction() -> None:
    calls: list[str] = []
    role = AgentRole(id="worker", system_prompt="Work.")

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> object:
        calls.append(message)
        detail = "provider rejected schema"
        raise AgentOutputSchemaError(detail)

    async def scenario() -> None:
        owner = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        try:
            with pytest.raises(SessionResumeError, match="provider checkpoint is missing"):
                await structured_turn(session, "work", _Reply, invocation_id="initial")
            outcome = session.inspect("initial")
            assert isinstance(outcome, InvalidResponse)
            assert outcome.checkpoint is None
            assert calls == ["work"]
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_fake_restart_before_correction_refuses_missing_conversation_history() -> None:
    calls: list[str] = []
    store = FakeAgentInvocationStore()
    role = AgentRole(id="worker", system_prompt="Work.")

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> object:
        calls.append(message)
        return {"value": "invalid"} if len(calls) == 1 else {"value": 7}

    async def scenario() -> None:
        for restarted in (False, True):
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
                if not restarted:
                    with pytest.raises(StructuredResponseError):
                        await session.turn("work", response=_Reply, invocation_id="initial")
                    assert isinstance(session.inspect("initial"), Completed)
                else:
                    with pytest.raises(
                        SessionResumeError, match="conversation history is unavailable"
                    ):
                        await structured_turn(session, "work", _Reply, invocation_id="initial")
                assert calls == ["work"]
            finally:
                await owner.close()

    asyncio.run(scenario())


def test_fake_unknown_correction_preserves_payload_fence(tmp_path: Path) -> None:
    calls: list[str] = []
    role = AgentRole(id="worker", system_prompt="Work.")

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> object:
        calls.append(message)
        if len(calls) == 1:
            return {"value": 7}
        detail = "provider acknowledgement lost"
        raise OSError(detail)

    async def scenario() -> None:
        owner = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        try:
            await session.turn("warmup", response=_Reply, invocation_id="warmup")
            message = TemplateRenderer(tmp_path).render_string("Correction.")
            first = await session.resume(message, "correction", response=_Reply)
            assert isinstance(first, Unknown)
            assert first.checkpoint is not None
            assert await session.resume(message, "correction", response=_Reply) == first
            changed = TemplateRenderer(tmp_path).render_string("Changed correction.")
            with pytest.raises(InvocationConflictError, match="payload changed"):
                await session.resume(changed, "correction", response=_Reply)
            assert calls == ["warmup", "Correction."]
        finally:
            await owner.close()

    asyncio.run(scenario())


def test_fake_native_schema_failure_retains_existing_checkpoint() -> None:
    calls: list[str] = []
    role = AgentRole(id="worker", system_prompt="Work.")

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> object:
        calls.append(message)
        if len(calls) == 2:
            detail = "provider rejected schema"
            raise AgentOutputSchemaError(detail)
        return {"value": len(calls)}

    async def scenario() -> None:
        owner = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        try:
            await session.turn("warmup", response=_Reply, invocation_id="warmup")
            checkpoint = session.checkpoint()
            assert await structured_turn(
                session, "work", _Reply, invocation_id="initial"
            ) == _Reply(value=3)
            rejected = session.inspect("initial")
            assert isinstance(rejected, InvalidResponse)
            assert rejected.checkpoint == checkpoint == session.checkpoint()
            assert len(calls) == 3
        finally:
            await owner.close()

    asyncio.run(scenario())


class _FailedCorrectionCommit(FakeAgentInvocationStore):
    def save(self, model: AgentInvocationState) -> None:
        record = model.invocations.get("correction")
        if record is not None and isinstance(record.outcome, Completed):
            detail = "correction journal unavailable"
            raise OSError(detail)
        super().save(model)


def test_fake_correction_commit_failure_propagates_without_provider_replay(tmp_path: Path) -> None:
    calls: list[str] = []
    role = AgentRole(id="worker", system_prompt="Work.")

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> object:
        calls.append(message)
        return {"value": 7}

    async def scenario() -> None:
        owner = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        owner.bind_invocation_store(_FailedCorrectionCommit())
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        try:
            await session.turn("warmup", response=_Reply, invocation_id="warmup")
            message = TemplateRenderer(tmp_path).render_string("Correction.")
            with pytest.raises(SessionPersistenceError, match="correction journal unavailable"):
                await session.resume(message, "correction", response=_Reply)
            outcome = await session.resume(message, "correction", response=_Reply)
            assert isinstance(outcome, Unknown)
            assert calls == ["warmup", "Correction."]
        finally:
            await owner.close()

    asyncio.run(scenario())
