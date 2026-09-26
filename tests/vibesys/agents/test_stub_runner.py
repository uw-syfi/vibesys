from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api import AgentSessionKey, SessionScope
from vs_agent.stub_runner import StubAgentClient

T = TypeVar("T", bound=BaseModel)


def test_stub_runner_returns_valid_agent_loop_responses(tmp_path: Path) -> None:
    runner = StubAgentClient()

    pre_round = invoke(runner, tmp_path, "orchestrator", PreRoundDecision)
    plan = invoke(runner, tmp_path, "orchestrator", OrchestratorPlan)
    implementation = invoke(runner, tmp_path, "implementer", ImplementerResponse)
    judgment = invoke(runner, tmp_path, "judge", JudgeResponse)

    assert pre_round.need_profile is False
    assert plan.task
    assert implementation.summary
    assert judgment.verdict is Verdict.PASS


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


def invoke(runner: StubAgentClient, workspace: Path, kind: str, response_cls: type[T]) -> T:
    def fallback() -> T:
        return response_cls.model_construct()

    return runner.invoke(
        kind=kind,
        workspace=workspace,
        system_prompt="system",
        user_prompt="user",
        response_cls=response_cls,
        fallback_factory=fallback,
        round_label=f"stub-{kind}",
    )


def test_stub_runner_names_no_provider_conversation() -> None:
    runner = StubAgentClient()
    key = AgentSessionKey(SessionScope.CHAT, "thread-a")

    # The stub runs no provider, so nothing can be resumed or compared against.
    assert runner.provider_session_id(key) is None
    assert runner.last_turn_provider_session_id(key) is None
