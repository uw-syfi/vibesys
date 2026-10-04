"""Trusted evaluations an implementer submitted reach its judge and its next attempt."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.support.evaluation_scenarios import ScenarioOutcome, ScenarioSpec, capture_projection
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    dynamic_options,
    implementation,
    portfolio,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_evaluation.api import EvidenceKind
from vs_runtime.api import (
    AgentCapability,
    AgentEvaluation,
    RunFacts,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_CAUSE = "ValueError: sequence length 22 exceeds state capacity 21"


def _produced_evaluation(
    revision: str,
    failure: str | None = None,
    kinds: tuple[EvidenceKind, ...] = (EvidenceKind.ACCURACY,),
) -> AgentEvaluation:
    return capture_projection(
        ScenarioSpec(
            revision=revision,
            kinds=kinds,
            outcome=ScenarioOutcome.CORRECTNESS_FAIL
            if failure is not None
            else ScenarioOutcome.PASS,
            failure=failure,
        )
    )


def _failed(failure: str, revision: str = "r-failed") -> AgentEvaluation:
    return _produced_evaluation(revision, failure)


def _run_with_submissions(
    tmp_path: Path,
    script: Script,
    submissions: list[list[AgentEvaluation]],
    **options: object,
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
                workspace.add_retained_revision(evaluation.revision)
                run.workspaces.retain_candidate_revision(evaluation.revision)
                run.workspaces.set_patch(evaluation.revision, ScenarioSpec().patch)
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
                AgentCapability.DURABLE_TURN_CONTINUATION,
            },
        )
        holder.append(run)
        await PLUGIN.orchestrate(
            run, dynamic_options(max_in_flight=1, max_retries_per_round=2, **options)
        )
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
    passed = _produced_evaluation("r-passed")
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


def _signed(signature: str | None, line: int = 442) -> AgentEvaluation:
    # The repeat classifier consumes supplied signatures, including missing and
    # distinct ones. The measurement and projection still come from the producer.
    return _failed(f"Traceback ... line {line}\n{_CAUSE}", f"r-{line}").model_copy(
        update={"signature": signature}
    )


def _repeat_script() -> Script:
    return Script(
        {
            ORCHESTRATOR.id: [portfolio("cache")],
            IMPLEMENTER.id: [implementation("cache"), implementation("cache-fixed")],
            JUDGE.id: [
                {"passed": True, "analysis": "Correct."},
                {"passed": True, "analysis": "Correct."},
            ],
        }
    )


def test_identical_failures_end_the_attempt_with_the_error_as_feedback(tmp_path: Path) -> None:
    signature = "ValueError at model.py:442"
    script = _repeat_script()

    _run_with_submissions(tmp_path, script, [[_signed(signature)] * 3, []])

    # The repeating candidate is neither reviewed nor gated; the retry is.
    assert len(_messages(script, JUDGE.id)) == 1
    retry = _messages(script, IMPLEMENTER.id)[1]
    assert f"3 evaluations in a row failed with the same error ({signature})" in retry
    assert _CAUSE in retry
    assert "state the cause" in retry


@pytest.mark.parametrize(
    ("submitted", "options"),
    [
        pytest.param(["sig"] * 3, {"max_repeated_failures": 4}, id="below-the-limit"),
        pytest.param(["sig", "other", "sig"], {}, id="different-signatures"),
        pytest.param([None] * 3, {}, id="no-signature"),
    ],
)
def test_failures_that_do_not_repeat_up_to_the_limit_keep_the_attempt(
    tmp_path: Path, submitted: list[str | None], options: dict[str, object]
) -> None:
    script = _repeat_script()

    _run_with_submissions(tmp_path, script, [[_signed(item) for item in submitted]], **options)

    assert len(_messages(script, IMPLEMENTER.id)) == 1
    assert len(_messages(script, JUDGE.id)) == 1
