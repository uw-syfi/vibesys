"""Profile single strategy plan correction and combined role handoff."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from vibesys.agent_run.attempts import AttemptState
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.options import AgentOrchestrationOptions
from vibesys.agent_run.state import AgentRunState
from vibesys.constants import DomainName
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.loops.profile_single.hypothesis import HypothesisEngine
from vibesys.loops.profile_single.session import AttemptRequest, PlanRequest
from vibesys.loops.profile_single.turns import InvalidPlanError, ProfileSingleTurns
from vibesys.profilers import ProfilerKind
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.search.hypothesis import OrchestratorPlan

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext


def _plan(hypothesis_id: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        title="Decode batching.",
        hypothesis="Batching reduces decode overhead",
        task="Batch decode requests",
        pass_criteria="Accuracy passes",  # noqa: S106
        reasoning="Decode is the bottleneck",
    )


def _response() -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="Implemented batching",
        expected_behavior="Lower latency",
        self_review="Accuracy checked",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="Decode",
        suggestions="Batch more",
        profile_analysis="Decode dominates",
    )


def _engine(state: AgentRunState) -> HypothesisEngine:
    return HypothesisEngine.create(state, config=ProfileGuidedInput(command=("true",)))


def _configured_turns(tmp_path: Path) -> ProfileSingleTurns:
    view = SimpleNamespace(
        paths=SimpleNamespace(
            objective="OBJECTIVE.md", benchmark_command=None, accuracy_command=None
        ),
        prompt_notes="Run locally",
        profile_execution="local",
    )
    context = SimpleNamespace(
        workspaces=SimpleNamespace(root=SimpleNamespace(path=tmp_path)),
        request=SimpleNamespace(
            objective="Reduce decode latency",
            input_bundle=SimpleNamespace(
                domain=DomainName.GENERIC,
                objective="Fallback objective",
                benchmark_result=None,
                benchmark_result_protocol=None,
            ),
        ),
        environment=SimpleNamespace(
            view=view,
            reference_path="reference.py",
            workspace_sources=(),
            profiler_kind=ProfilerKind.NONE,
        ),
    )
    options = AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=2,
        max_retries_per_round=2,
        judge_every=1,
        official_eval_every=2,
        memory_layout="files",
        profile_guided=ProfileGuidedInput(command=("profile",)),
    )
    return ProfileSingleTurns(cast("RunContext", context), options)


def test_prompts_render_own_strategy_root_and_official_planning_context(tmp_path: Path) -> None:
    turns = _configured_turns(tmp_path)
    plan = _plan("h1")
    engine = _engine(AgentRunState()).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    plan_request = PlanRequest(
        1, engine.state, [], CarryOver(), None, None, 1, engine.controller.guidance
    )

    designer_prompt = turns._plan_prompt(plan_request)  # noqa: SLF001
    combined_prompt = turns._combined_prompt(  # noqa: SLF001
        AttemptRequest(1, plan, "profile-guided component measurement", [], hypothesis, "decode"),
        AttemptState(agent_run_state=engine.state, feedback=None, retry=1),
        [],
    )

    assert turns.template_dir.name == "profile_single"
    assert "OBJECTIVE.md" in designer_prompt
    assert "Run locally" in designer_prompt
    assert "OBJECTIVE.md" in combined_prompt
    assert "profile-guided component measurement" in combined_prompt
    assert "Batch decode requests" not in combined_prompt
    assert (tmp_path / "progress-artifacts" / "plans" / "round-0001.json").exists()


@pytest.mark.asyncio
async def test_plan_reprompts_reused_hypothesis_and_records_corrected_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = _engine(AgentRunState()).start(_plan("used"), started_round=1)
    state = prior.state
    turns = ProfileSingleTurns.__new__(ProfileSingleTurns)
    log: list[str] = []
    monkeypatch.setattr(turns, "ctx", SimpleNamespace(log=log.append), raising=False)
    monkeypatch.setattr(turns, "progress_path", tmp_path / "progress.md", raising=False)
    monkeypatch.setattr(turns, "_plan_prompt", lambda _request: "plan prompt")
    designer = AsyncMock(side_effect=[_plan("used"), _plan("new")])
    monkeypatch.setattr(turns, "_designer_turn", designer)
    monkeypatch.setattr(turns, "_skills", lambda selections: (selections, []))
    request = PlanRequest(
        2, state, state.rounds, CarryOver(), None, None, 0, prior.controller.guidance
    )

    plan = await turns.plan(request)

    assert plan.hypothesis_id == "new"
    assert designer.await_count == 2
    assert "previous plan was rejected" in designer.await_args_list[1].args[1]
    assert "rejected" in log[0]
    assert (tmp_path / "progress-artifacts" / "plans" / "round-0002.json").exists()


@pytest.mark.asyncio
async def test_combined_turn_records_response_and_uses_hypothesis_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan("h1")
    engine = _engine(AgentRunState()).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    turns = ProfileSingleTurns.__new__(ProfileSingleTurns)
    worker = SimpleNamespace(turn_structured=AsyncMock(return_value=_response()))
    workspace = SimpleNamespace(snapshot=AsyncMock(return_value="revision"))
    monkeypatch.setattr(turns, "worker", worker, raising=False)
    monkeypatch.setattr(turns, "workspace", workspace, raising=False)
    monkeypatch.setattr(turns, "progress_path", tmp_path / "progress.md", raising=False)
    monkeypatch.setattr(turns, "_skills", lambda selections: (selections, []))
    monkeypatch.setattr(turns, "_combined_prompt", lambda *_args: "combined prompt")
    request = AttemptRequest(1, plan, None, [], hypothesis, "decode")
    attempt = AttemptState(agent_run_state=engine.state, feedback=None, retry=1)

    response = await turns.combined(request, attempt)

    assert response.verdict is Verdict.PASS
    assert worker.turn_structured.await_args.kwargs["system_prompt"] == "combined prompt"
    assert worker.turn_structured.await_args.kwargs["label"] == "round-1-retry-1-single-agent"
    workspace.snapshot.assert_awaited_once_with("round-1-retry-1-single-agent")
    assert "Implemented batching" in (tmp_path / "progress.md").read_text()


def test_validation_rejects_reused_id() -> None:
    plan = _plan("used")
    state = _engine(AgentRunState()).start(plan, started_round=1).state
    turns = ProfileSingleTurns.__new__(ProfileSingleTurns)

    with pytest.raises(InvalidPlanError, match="already used"):
        turns._validate_plan(_plan("used"), state)  # noqa: SLF001
