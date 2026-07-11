from vibe_serve.agents.stub_runner import StubAgentRunner
from vibe_serve.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    Verdict,
)


def test_stub_runner_returns_valid_agent_loop_responses(tmp_path):
    runner = StubAgentRunner()

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


def invoke(runner, workspace, kind, response_cls):
    return runner.invoke(
        kind=kind,
        workspace=workspace,
        system_prompt="system",
        user_prompt="user",
        response_cls=response_cls,
        fallback_factory=lambda: None,
        round_label=f"stub-{kind}",
    )
