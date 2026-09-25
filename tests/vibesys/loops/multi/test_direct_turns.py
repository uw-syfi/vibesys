"""Multi role ordering, isolation, and durable role evidence."""

# ruff: noqa: SLF001  # LW-030007; Read-only guards and role turns are the policy under test.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.support import make_orchestrator_plan

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import AttemptState
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.options import AgentOrchestrationOptions
from vibesys.agent_run.state import AgentRunState
from vibesys.constants import DomainName
from vibesys.loops.multi.decisions import (
    AttemptRequest,
    HypothesisEngine,
    PlainGuidance,
    PlanRequest,
)
from vibesys.loops.multi.turns import (
    InvalidPlanError,
    MultiAgentTurns,
    RoleIsolationError,
    UnsupportedProfilerError,
    _unauthorized_paths,
)
from vibesys.profilers import ProfilerKind
from vibesys.schemas import (
    HypothesisOutcome,
    HypothesisStrategyUpdate,
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SkillResourceSelection,
    Verdict,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext


def _options() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=2,
        max_retries_per_round=2,
        judge_every=1,
        official_eval_every=2,
        memory_layout="files",
    )


def _plan(*, hypothesis_id: str = "h1") -> OrchestratorPlan:
    return make_orchestrator_plan(
        hypothesis_id=hypothesis_id,
        hypothesis="Cache decode",
        title="Decode cache",
        task="Implement the cache",
        criteria="Candidate still responds",
        reasoning="Decode is expensive",
    )


def _turns(tmp_path: Path) -> MultiAgentTurns:
    workspace = SimpleNamespace(
        path=tmp_path,
        revision="a" * 40,
        snapshot=AsyncMock(return_value="a" * 40),
        pending_changes=AsyncMock(return_value=[]),
        restore=AsyncMock(),
    )
    bundle = SimpleNamespace(
        domain=DomainName.GENERIC,
        objective="Improve throughput",
        benchmark_result=None,
        benchmark_result_protocol=None,
    )
    view = SimpleNamespace(
        prompt_notes="Local runtime",
        profile_execution="Run a bounded profile",
        paths=SimpleNamespace(
            objective="OBJECTIVE.md",
            benchmark_command="python bench.py",
            accuracy_command="python check.py",
        ),
    )
    ctx = cast(
        "RunContext",
        SimpleNamespace(
            workspaces=SimpleNamespace(root=workspace),
            request=SimpleNamespace(input_bundle=bundle, objective=None),
            environment=SimpleNamespace(
                view=view,
                profiler_kind=ProfilerKind.MACOS_CPU,
                reference_path="reference/",
                workspace_sources=(),
                skill_source_paths=(),
            ),
            events=SimpleNamespace(emit=MagicMock()),
            log=MagicMock(),
        ),
    )
    turns = MultiAgentTurns(ctx, _options())
    issue_board.ensure_progress_file(turns.progress_path)
    issue_board.ensure_roadmap_file(turns.roadmap_path)
    return turns


def _request() -> tuple[PlanRequest, AttemptRequest, AttemptState]:
    state = AgentRunState()
    plan = _plan()
    engine = HypothesisEngine.create(state).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    return (
        PlanRequest(1, state, [], CarryOver(), None, None, 0, PlainGuidance()),
        AttemptRequest(1, plan, "final_round", [], hypothesis, engine, "decode"),
        AttemptState(agent_run_state=engine.state, feedback=None, retry=1),
    )


def _handle(result: object) -> SimpleNamespace:
    return SimpleNamespace(turn_structured=AsyncMock(return_value=result), close=AsyncMock())


def test_roles_open_close_and_render_profile_guided_plan(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    handles = {
        "orchestrator": _handle(_plan()),
        "implementer": _handle(ImplementerResponse(summary="done", expected_behavior="faster")),
        "judge": _handle(JudgeResponse(analysis="ok", feedback="", verdict=Verdict.PASS)),
        "profiler": _handle(
            ProfilerSummary(analysis="ok", bottlenecks="decode", suggestions="cache")
        ),
    }
    turns.ctx.agents = SimpleNamespace(
        default_definition=lambda role: role,
        spawn=AsyncMock(side_effect=lambda role: handles[role]),
    )
    asyncio.run(turns.open())
    assert turns.ctx.agents.spawn.await_count == 4

    plan_request, _, _ = _request()
    prompt = turns._plan_prompt(plan_request)
    assert "Orchestrator" in prompt
    planned = asyncio.run(turns.plan(plan_request))
    assert planned.hypothesis_id == "h1"
    assert (tmp_path / "progress-artifacts" / "plans" / "round-0001.json").is_file()

    asyncio.run(turns.close())
    assert all(handle.close.await_count == 1 for handle in handles.values())


def test_plan_reprompts_reused_id_then_accepts_new_one(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    existing = HypothesisEngine.create(AgentRunState()).start(_plan(), started_round=1).state
    request = PlanRequest(2, existing, [], CarryOver(), None, None, 0, PlainGuidance())
    turns._designer_turn = AsyncMock(side_effect=[_plan(), _plan(hypothesis_id="h2")])
    result = asyncio.run(turns.plan(request))
    assert result.hypothesis_id == "h2"
    assert turns._designer_turn.await_count == 2
    assert turns._designer_turn.await_args is not None
    assert "rejected" in turns._designer_turn.await_args.args[1]


def test_plan_rejects_duplicate_self_and_existing_hypothesis_updates(tmp_path: Path) -> None:
    turns = _turns(tmp_path)
    state = AgentRunState()
    update = HypothesisStrategyUpdate(
        hypothesis_id="old", disposition="parked", reason="Not current"
    )
    with pytest.raises(InvalidPlanError, match="more than once"):
        turns._validate_plan(
            _plan().model_copy(update={"hypothesis_updates": [update, update]}), state
        )
    with pytest.raises(InvalidPlanError, match="new hypothesis"):
        turns._validate_plan(
            _plan().model_copy(
                update={"hypothesis_updates": [update.model_copy(update={"hypothesis_id": "h1"})]}
            ),
            state,
        )
    existing = HypothesisEngine.create(state).start(_plan(), started_round=1).state
    with pytest.raises(InvalidPlanError, match="already used"):
        turns._validate_plan(_plan(), existing)


def test_designer_and_read_only_roles_verify_restoration(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    turns.designer = _handle(_plan())
    turns.workspace.pending_changes.side_effect = [["candidate.py"], []]
    assert asyncio.run(turns._designer_turn("prompt", "message", "plan")).hypothesis_id == "h1"
    turns.workspace.restore.assert_awaited_once()

    turns.workspace.pending_changes.side_effect = [["candidate.py"], ["candidate.py"]]
    with pytest.raises(RoleIsolationError, match="still modified"):
        asyncio.run(turns._designer_turn("prompt", "message", "plan"))

    assert _unauthorized_paths(["profile/log.txt", "candidate.py"], ("profile/",)) == [
        "candidate.py"
    ]
    turns.workspace.pending_changes.side_effect = [["profile/log.txt", "candidate.py"], []]
    decision = PreRoundDecision(need_profile=False, profile_focus="", reasoning="enough")
    assert (
        asyncio.run(
            turns._read_only(
                _handle(decision),
                message="decide",
                prompt="prompt",
                response_cls=PreRoundDecision,
                fallback_factory=lambda: decision,
                label="pre",
                allowed=("profile/",),
            )
        )
        == decision
    )


def test_pre_round_profile_and_profiler_guards(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    turns.designer = _handle(
        PreRoundDecision(need_profile=True, profile_focus="decode", reasoning="measure")
    )
    decision = asyncio.run(turns.pre_round_decision(1, CarryOver(), has_history=False))
    assert decision.need_profile
    assert decision.profile_focus == "decode"

    turns.profiler = _handle(
        ProfilerSummary(analysis="profiled", bottlenecks="decode", suggestions="cache")
    )
    summary = asyncio.run(turns.profile(1, "decode"))
    assert summary is not None
    assert summary.analysis == "profiled"
    assert turns.profiler.turn_structured.await_args.kwargs["mcp_servers"]

    turns.ctx.environment.profiler_kind = ProfilerKind.NONE
    assert asyncio.run(turns.profile(1, "decode")) is None
    turns.ctx.environment.profiler_kind = ProfilerKind.TORCH
    with pytest.raises(UnsupportedProfilerError):
        turns._profiler()


def test_implementer_marks_paid_turn_after_prompt_and_judge_applies_pareto_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns = cast("Any", _turns(tmp_path))
    _, request, attempt = _request()
    response = ImplementerResponse(
        summary="cache added",
        expected_behavior="faster",
        hypothesis_outcome=HypothesisOutcome.SUPPORTED,
    )
    turns.worker = _handle(response)
    events: list[str] = []
    original_marker = issue_board.write_implementer_start_marker

    def marker(path: Path, round_number: int, retry: int) -> Path:
        events.append("marker")
        return original_marker(path, round_number, retry)

    original_prompt = turns._implementer_prompt

    def prompt(req: AttemptRequest, state: AttemptState, skills: list) -> str:
        events.append("prompt")
        return original_prompt(req, state, skills)

    async def worker_turn(_message: str, **_kwargs: object) -> ImplementerResponse:
        events.append("worker")
        return response

    monkeypatch.setattr(
        "vibesys.loops.multi.turns.issue_board.write_implementer_start_marker", marker
    )
    turns._implementer_prompt = prompt
    turns.worker.turn_structured = AsyncMock(side_effect=worker_turn)
    returned, synthesized = asyncio.run(turns.implement(request, attempt))
    assert returned == response
    assert not synthesized
    assert events == ["prompt", "marker", "worker"]
    assert turns.worker.turn_structured.await_args is not None
    assert turns.worker.turn_structured.await_args.kwargs["reuse_session"] is True

    attempt.implementation = response
    turns.judge = _handle(JudgeResponse(analysis="plausible", feedback="", verdict=Verdict.PASS))
    verdict = asyncio.run(turns.review(request, attempt, "conflicts with retained frontier"))
    assert verdict.verdict is Verdict.FAIL
    assert "Pareto guard" in verdict.analysis
    turns.ctx.events.emit.assert_called()


def test_missing_implementation_and_unavailable_skills_fail_closed(tmp_path: Path) -> None:
    turns = _turns(tmp_path)
    _, request, attempt = _request()
    with pytest.raises(ValueError, match="parsed implementer"):
        asyncio.run(turns.review(request, attempt, None))
    selections = [SkillResourceSelection(skill="missing", purpose="Need guidance")]
    assert turns._skills(selections) == ([], [])
