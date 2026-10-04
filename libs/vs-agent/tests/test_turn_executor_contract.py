"""The raw durable-turn interface is shared by production and scripted clients."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from vs_agent.api import (
    AgentCapabilities,
    AgentClient,
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnExecutor,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    MCPServerSpec,
    SessionResumeError,
    SessionScope,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore, FakeDriver
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from pathlib import Path


class Reply(BaseModel):
    value: int


@dataclass
class Observer:
    events: list[AgentEvent] = field(default_factory=list)

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)


@pytest.mark.parametrize("implementation", ["client", "fake"])
def test_executor_initial_and_strict_continuation_share_one_conversation(
    tmp_path: Path, implementation: str
) -> None:
    calls: list[object] = []
    if implementation == "client":
        executor = AgentClient(
            FakeDriver(
                answer={"value": 7},
                on_turn=calls.append,
                turn=[AgentEvent(kind=AgentEventKind.TEXT, text='{"value":7}')],
            )
        )
    else:
        executor = FakeAgentClient(
            capabilities=AgentCapabilities(provider_session_resume=True, session_reuse=True)
        )
        executor.set_response("worker", {"value": 7}).on_invoke(calls.append)
        executor.stream_output("worker", ['{"value":7}'])
    assert isinstance(executor, AgentTurnExecutor)
    key = AgentSessionKey(SessionScope.MEMBER, "worker:member")
    spec = AgentSessionSpec(
        role="worker",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
        mcp_servers=(MCPServerSpec("diagnostic", "diagnostic", runtime_env=(("TOKEN", "fresh"),)),),
    )
    initial = AgentTurnRequest(message="work", output_schema=Reply, invocation_id="initial")
    sessions = ClientAgentSessions(executor, FakeAgentInvocationStore())
    try:
        first = sessions.start(key, spec, initial)
        assert isinstance(first, Completed)
        assert Reply.model_validate_json(first.result.text) == Reply(value=7)
        sessions.bind(key, spec, replace(initial, message="", invocation_id=None))
        message = TemplateRenderer(tmp_path).render_string("trusted result")
        resumed = sessions.resume(key, message, "resume")
        assert isinstance(resumed, Completed)
        assert resumed.checkpoint == first.checkpoint
        assert len(calls) == 2
        if isinstance(executor, FakeAgentClient):
            assert executor.calls[0].tool_servers is not None
            assert executor.calls[0].tool_servers[0].runtime_env == (("TOKEN", "fresh"),)
        strict = replace(initial, expected_provider_session_id=first.checkpoint.provider_session_id)
        with pytest.raises(SessionResumeError, match="specification changed"):
            executor.run(session_spec=replace(spec, model="changed"), turn=strict, session_key=key)
        assert len(calls) == 2
        with pytest.raises(SessionResumeError, match="identity changed"):
            executor.run(
                session_spec=spec,
                turn=replace(strict, expected_provider_session_id="other"),
                session_key=key,
            )
        assert len(calls) == 2
        observer = Observer()
        result = executor.run(session_spec=spec, turn=strict, session_key=key, observer=observer)
        assert result.provider_session_id == first.checkpoint.provider_session_id
        text = "".join(
            event.text or "" for event in observer.events if event.kind is AgentEventKind.TEXT
        )
        assert Reply.model_validate_json(text) == Reply(value=7)
        assert len(calls) == 3
    finally:
        executor.close()
