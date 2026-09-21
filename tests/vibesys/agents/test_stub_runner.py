from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    Verdict,
)
from vs_agent.api import AgentSessionKey, SessionScope
from vs_agent.stub_runner import StubAgentClient


def test_stub_runner_returns_valid_agent_loop_responses(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    runner = StubAgentClient()

    responses = [
        invoke(runner, tmp_path, "orchestrator", PreRoundDecision),
        invoke(runner, tmp_path, "orchestrator", OrchestratorPlan),
        invoke(runner, tmp_path, "implementer", ImplementerResponse),
        invoke(runner, tmp_path, "judge", JudgeResponse),
    ]

    assert responses[0].need_profile is False
    assert responses[1].task
    assert responses[2].summary
    assert responses[3].verdict is Verdict.PASS


def test_stub_runner_returns_plain_chat_text(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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


def invoke(runner, workspace, kind, response_cls):  # noqa: ANN001, ANN201  # tracked: #288
    return runner.invoke(
        kind=kind,
        workspace=workspace,
        system_prompt="system",
        user_prompt="user",
        response_cls=response_cls,
        fallback_factory=lambda: None,
        round_label=f"stub-{kind}",
    )


def test_stub_runner_names_no_provider_conversation():  # noqa: ANN201
    runner = StubAgentClient()
    key = AgentSessionKey(SessionScope.CHAT, "thread-a")

    # The stub runs no provider, so nothing can be resumed or compared against.
    assert runner.provider_session_id(key) is None
    assert runner.last_turn_provider_session_id(key) is None
