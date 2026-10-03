"""Trusted evaluations an implementer submitted reach its judge and its next attempt."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._support import (
    Script,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_runtime.api import (
    AgentCapability,
    AgentEvaluation,
    AgentEvaluationStatus,
    RunFacts,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_CAUSE = "ValueError: sequence length 22 exceeds state capacity 21"


def _failed(failure: str) -> AgentEvaluation:
    return AgentEvaluation(
        revision="r-failed",
        kinds=("accuracy",),
        status=AgentEvaluationStatus.FAILED,
        failure=failure,
    )


def _run_with_submissions(
    tmp_path: Path, script: Script, submissions: list[list[AgentEvaluation]]
) -> FakeRun:
    """Run one workstream whose n-th implementer turn submits ``submissions[n]``."""
    turns = iter(submissions)
    holder: list[FakeRun] = []

    def respond(
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        if role.id == IMPLEMENTER.id:
            run = holder[0]
            workspace = run.workspaces.candidates[-1]
            for evaluation in next(turns, []):
                run.evaluation.record_agent_evaluation(workspace, evaluation)
        return script.respond(role, history, message, response)

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Improve.", accuracy_configured=True),
            responder=respond,
            supported_extra_tools={"evaluation", "profiler"},
            supports_parallel_candidates=True,
            supported_agent_capabilities={
                AgentCapability.MCP_SERVERS,
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            },
        )
        holder.append(run)
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1, max_retries_per_round=2))
        return run

    return asyncio.run(scenario())


def _messages(script: Script, role_id: str) -> list[str]:
    return [message for role, _, message in script.calls if role == role_id]


def test_the_judge_sees_the_candidates_evaluation_failures_and_the_retry_inherits_them(
    tmp_path: Path,
) -> None:
    # A long failure: only its end, which states the cause, may reach the prompts.
    failure = "HEAD-OF-A-LONG-LOG\n" + "noise line\n" * 2000 + _CAUSE
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache"), implementation("cache-fixed")],
            JUDGE.id: [
                {"passed": False, "analysis": "Fails its evaluation.", "feedback": "Fix it."},
                {"passed": True, "analysis": "Correct."},
            ],
        }
    )

    _run_with_submissions(tmp_path, script, [[_failed(failure)], []])

    review = _messages(script, JUDGE.id)[0]
    assert "revision `r-failed`, accuracy: failed" in review
    assert _CAUSE in review
    assert "HEAD-OF-A-LONG-LOG" not in review
    retry = _messages(script, IMPLEMENTER.id)[1]
    assert "Fix it." in retry
    assert _CAUSE in retry
    assert "HEAD-OF-A-LONG-LOG" not in retry
    assert len(review) < len(failure)


def test_a_retry_carries_only_the_failures_of_the_attempt_before_it(tmp_path: Path) -> None:
    passed = AgentEvaluation(
        revision="r-passed", kinds=("accuracy",), status=AgentEvaluationStatus.PASSED
    )
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache"), implementation("cache-fixed")],
            JUDGE.id: [
                {"passed": False, "analysis": "Unsupported.", "feedback": "Show evidence."},
                {"passed": True, "analysis": "Correct."},
            ],
        }
    )

    _run_with_submissions(tmp_path, script, [[passed], []])

    retry = _messages(script, IMPLEMENTER.id)[1]
    assert "Show evidence." in retry
    assert "submitted in that attempt failed" not in retry
    assert "revision `r-passed`, accuracy: passed" in _messages(script, JUDGE.id)[0]
