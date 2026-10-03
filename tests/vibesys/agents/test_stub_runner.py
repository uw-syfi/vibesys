from pathlib import Path

import pytest
from pydantic import BaseModel

from vs_agent.api import AgentOutputSchemaError, AgentSessionKey, SessionScope
from vs_agent.stub_runner import StubAgentClient


class _Response(BaseModel):
    value: int


def test_stub_runner_reports_a_structured_turn_as_schema_failure(tmp_path: Path) -> None:
    runner = StubAgentClient()

    with pytest.raises(AgentOutputSchemaError, match="no structured output"):
        runner.invoke(
            kind="worker",
            workspace=tmp_path,
            system_prompt="system",
            user_prompt="user",
            response_cls=_Response,
            round_label="stub-worker",
        )


def test_stub_runner_returns_plain_chat_text(tmp_path: Path) -> None:
    runner = StubAgentClient()

    answer = runner.invoke_text(
        kind="chat",
        workspace=tmp_path,
        system_prompt="investigate",
        user_prompt="what happened?",
        round_label="experiment-chat",
        invocation_id="chat-1",
    )

    assert answer == "Stub agent inspected the available experiment trajectory."


def test_stub_runner_names_no_provider_conversation() -> None:
    runner = StubAgentClient()
    key = AgentSessionKey(SessionScope.CHAT, "thread-a")

    # The stub runs no provider, so nothing can be resumed or compared against.
    assert runner.provider_session_id(key) is None
    assert runner.last_turn_provider_session_id(key) is None


def test_stub_runner_emulates_builtin_conversation_capabilities() -> None:
    capabilities = StubAgentClient().capabilities

    assert capabilities.session_reuse
    assert capabilities.provider_session_resume
    assert capabilities.tool_servers
