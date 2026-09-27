"""Single combined-role policy through the public runtime Fake."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.hypothesis import (
    AttemptState,
    HypothesisConfig,
    HypothesisSearch,
    HypothesisState,
    OrchestratorPlan,
)
from vibesys.orchestration.metrics import MetricSpace, Objective
from vibesys.orchestration.single import PLUGIN
from vibesys.orchestration.single.combined import CombinedTurnRequest, SingleAgentWorker
from vibesys.orchestration.single.models import (
    SingleAgentRoundContext,
    SingleAgentRoundResponse,
)
from vibesys.schemas import SkillResourceSelection, Verdict
from vs_loop_state.api import CandidateDisposition, RoundRecord
from vs_runtime.api import AgentTurnTimeoutError, StructuredResponseError
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole, Workspace


def _plan(
    hypothesis_id: str, *, skills: list[SkillResourceSelection] | None = None
) -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "hypothesis_id": hypothesis_id,
            "task": "Batch prefill requests.",
            "pass_criteria": "Throughput improves without accuracy loss.",
            "reasoning": "Repeated launch overhead is visible.",
            "recommended_skills": skills or [],
        }
    )


def _response(**changes: object) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse.model_validate(
        {
            "summary": "Implemented batching.",
            "expected_behavior": "Fewer launches.",
            "self_review": "Correctness checks passed.",
            "feedback": "",
            "verdict": Verdict.PASS,
            "bottlenecks": "Launch overhead.",
            "suggestions": "Try larger batches.",
            "profile_analysis": "Launch time fell.",
            **changes,
        }
    )


def _context(*, feedback: str | None = None) -> SingleAgentRoundContext:
    return SingleAgentRoundContext(
        accuracy_command="check-accuracy",
        benchmark_command="run-benchmark",
        domain_profiler="Capture a scoped profile.",
        domain_single_agent="Preserve the serving contract.",
        feedback=feedback,
        interface="Serve requests.",
        objective_location="OBJECTIVE.md",
        official_evaluation_due=False,
        official_evaluation_reason=None,
        pareto_archive_location="progress/pareto.md",
        plan_artifact_location="progress/plans/round-0001.json",
        profiler_kind="none",
        profiler_support_name=None,
        progress_location="progress/ledger.md",
        runtime_notes="Use the allocated device.",
        validation_location="progress/validation",
    )


def _request(
    workspace: Workspace,
    plan: OrchestratorPlan,
    *,
    attempt: AttemptState | None = None,
    records: tuple[RoundRecord, ...] = (),
) -> CombinedTurnRequest:
    attempt = attempt or AttemptState(agent_run_state=HypothesisState(), feedback=None)
    return CombinedTurnRequest(
        round_number=1,
        plan=plan,
        attempt=attempt,
        records=records,
        context=_context(feedback=attempt.feedback),
        workspace=workspace,
    )


class _Script:
    def __init__(self, *replies: object) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[AgentRole, tuple[str, ...], str, type[BaseModel] | None]] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role, history, message, response))
        value = self.replies.popleft()
        if isinstance(value, BaseException):
            raise value
        return value


def _host(script: _Script) -> FakeRunHost:
    return FakeRunHost(PLUGIN, project_root=Path("/candidate"), responder=script.respond)


def _search() -> HypothesisSearch:
    return HypothesisSearch(HypothesisConfig(max_rounds=3))


def test_retry_preserves_one_named_session_and_new_prompt_evidence() -> None:
    script = _Script(_response(verdict=Verdict.FAIL, feedback="Fix validation."), _response())
    host = _host(script)

    async def scenario() -> None:
        worker = SingleAgentWorker(host, _search())
        try:
            plan = _plan("H-01")
            first = await worker.turn(_request(host.workspaces.root, plan))
            second = await worker.turn(
                _request(
                    host.workspaces.root,
                    plan,
                    attempt=AttemptState(
                        agent_run_state=HypothesisState(),
                        feedback=first.feedback,
                        retry=1,
                    ),
                )
            )
            assert first.verdict is Verdict.FAIL
            assert second.verdict is Verdict.PASS
            assert len(host.agents.sessions) == 1
            assert host.agents.sessions[0].member_id == "H-01"
            assert [len(history) for _role, history, _message, _type in script.calls] == [0, 1]
            assert "Fix validation." in script.calls[1][2]
            assert all(call[3] is SingleAgentRoundResponse for call in script.calls)
        finally:
            await worker.close()
            await host.close()

    asyncio.run(scenario())
    assert host.agents.sessions[0].closed


def test_distinct_hypotheses_get_distinct_durable_identities_and_cleanup() -> None:
    script = _Script(_response(), _response())
    host = _host(script)

    async def scenario() -> None:
        worker = SingleAgentWorker(host, _search())
        try:
            await worker.turn(_request(host.workspaces.root, _plan("H-01")))
            await worker.turn(_request(host.workspaces.root, _plan("H-02")))
            assert len(host.agents.sessions) == 2
            identities = [session.member_id for session in host.agents.sessions]
            assert identities == ["H-01", "H-02"]
            assert [len(history) for _role, history, _message, _type in script.calls] == [0, 0]
        finally:
            await worker.close()
            await worker.close()
            await host.close()

        with pytest.raises(RuntimeError, match="closed"):
            await worker.turn(_request(host.workspaces.root, _plan("H-03")))

    asyncio.run(scenario())
    assert all(session.closed for session in host.agents.sessions)


def test_plan_recommendations_and_returned_skill_updates_are_resolved() -> None:
    initial = SkillResourceSelection(
        skill="profiling",
        resource_paths=["references/start.md", "missing.md"],
        purpose="Understand trace.",
    )
    new = SkillResourceSelection(
        skill="testing",
        resource_paths=["references/checks.md"],
        purpose="Check behavior.",
    )
    script = _Script(_response(skill_context_updates=[new]))
    host = _host(script)
    host.skills.installed_resources = {
        "profiling": ("SKILL.md", "references/start.md"),
        "testing": ("SKILL.md", "references/checks.md"),
    }
    plan = _plan("H-01", skills=[initial])

    async def scenario() -> SingleAgentRoundResponse:
        worker = SingleAgentWorker(host, _search())
        try:
            return await worker.turn(_request(host.workspaces.root, plan))
        finally:
            await worker.close()
            await host.close()

    response = asyncio.run(scenario())
    assert plan.recommended_skills == [
        SkillResourceSelection(
            skill="profiling",
            resource_paths=["references/start.md"],
            purpose="Understand trace.",
        ),
        new,
    ]
    assert response.skill_context_updates == [new]
    assert any("missing.md" in message for message in host.logs)


def test_pareto_guard_downgrades_dominated_pass_and_preserves_evidence() -> None:
    space = MetricSpace(
        objectives=(
            Objective(name="ops_per_sec", direction="max"),
            Objective(name="latency_ms", direction="min"),
        )
    )
    previous = RoundRecord(
        round_number=1,
        commit="a" * 40,
        passed=True,
        reviewed=True,
        hypothesis_id="H-00",
        hypothesis_outcome="proven",
        judge_verdict="pass",
        official_evaluation=True,
        perf_metric=100,
        perf_unit="ops_per_sec",
        perf_direction="max",
        perf_provenance="framework",
        metrics={"ops_per_sec": 100, "latency_ms": 50},
        candidate_metrics={"ops_per_sec": 100, "latency_ms": 50},
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_retained=True,
    )
    script = _Script(
        _response(
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
            candidate_metrics={"ops_per_sec": 80, "latency_ms": 70},
        )
    )
    host = _host(script)

    async def scenario() -> SingleAgentRoundResponse:
        worker = SingleAgentWorker(host, _search())
        try:
            return await worker.turn(
                _request(
                    host.workspaces.root,
                    _plan("H-01"),
                    records=(previous,),
                    attempt=AttemptState(
                        agent_run_state=HypothesisState(metrics=space), feedback=None
                    ),
                )
            )
        finally:
            await worker.close()
            await host.close()

    response = asyncio.run(scenario())
    assert response.verdict is Verdict.FAIL
    assert "dominated by round 1" in response.feedback
    assert "Framework Pareto guard" in response.self_review
    assert response.candidate_metrics == {"ops_per_sec": 80, "latency_ms": 70}


def test_unparseable_turn_yields_failure_and_session_is_cleaned_up() -> None:
    script = _Script(StructuredResponseError("implementer", SingleAgentRoundResponse))
    host = _host(script)

    async def scenario() -> SingleAgentRoundResponse:
        worker = SingleAgentWorker(host, _search())
        try:
            return await worker.turn(_request(host.workspaces.root, _plan("H-01")))
        finally:
            await worker.close()
            await host.close()

    response = asyncio.run(scenario())
    assert response.verdict is Verdict.FAIL
    assert "No structured response" in response.feedback
    assert host.agents.sessions[0].closed


def test_timed_out_turn_reports_budget_and_preserves_named_session_for_retry() -> None:
    script = _Script(AgentTurnTimeoutError(12.5), _response())
    host = _host(script)

    async def scenario() -> tuple[SingleAgentRoundResponse, SingleAgentRoundResponse]:
        worker = SingleAgentWorker(host, _search())
        try:
            plan = _plan("H-01")
            first = await worker.turn(_request(host.workspaces.root, plan))
            second = await worker.turn(
                _request(
                    host.workspaces.root,
                    plan,
                    attempt=AttemptState(
                        agent_run_state=HypothesisState(),
                        feedback=first.feedback,
                        retry=1,
                    ),
                )
            )
            return first, second
        finally:
            await worker.close()
            await host.close()

    first, second = asyncio.run(scenario())
    assert first.verdict is Verdict.FAIL
    assert "12.5 seconds" in first.self_review
    assert (
        first.feedback == "Inspect retained evidence and return a schema-valid response on retry."
    )
    assert second.verdict is Verdict.PASS
    assert len(host.agents.sessions) == 1
    assert host.agents.sessions[0].closed


def test_rejects_missing_hypothesis_identity_before_opening_session() -> None:
    host = _host(_Script())
    with pytest.raises(ValueError, match="hypothesis_id"):
        _request(host.workspaces.root, _plan(""))
    assert host.agents.sessions == ()
