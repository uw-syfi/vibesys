"""Multi role ordering and durable role evidence.

``vibesys.loops.multi.turns`` no longer renders prompts or handles isolation
itself (phase 3b): every role turn goes through ``ctx.agents.turn``, which is
covered directly in ``tests/vibesys/orchestration``. These tests cover what
stays strategy code: the context each role's turn is built with, the
designer's state-dependent plan-ID retry loop, paid-turn marker timing, the
implementer's synthesized-reply detection, and the judge's Pareto guard.
"""

from __future__ import annotations

# ruff: noqa: SLF001  # context builders and plan validation are the policy under test.
import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibesys.agent_run import issue_board
from vibesys.constants import DomainName
from vibesys.errors import (
    InvalidPlanError,
    MissingImplementationError,
    UnsupportedProfilerError,
)
from vibesys.loops.agent_options import AgentOrchestrationOptions
from vibesys.loops.multi.decisions import AttemptRequest, PlanRequest
from vibesys.loops.multi.turns import MultiAgentTurns
from vibesys.profilers import ProfilerKind
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import MULTI_IMPLEMENTER_CONTINUATION, ImplementerResponse
from vibesys.roles.judge import MULTI_JUDGE, JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.roles.profiler import ProfilerSummary
from vibesys.runtime import ReadOnly
from vibesys.schemas import (
    HypothesisOutcome,
)
from vibesys.search.hypothesis import (
    HypothesisConfig,
    HypothesisSearch,
    HypothesisStrategyUpdate,
    OrchestratorPlan,
)
from vibesys.search.hypothesis.attempts import AttemptState
from vibesys.search.hypothesis.state import HypothesisState
from vibesys.search.hypothesis.transitions import CarryOver
from vibesys.search.profile_focus import FocusView

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
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
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="Cache decode",
        title="Decode cache",
        task="Implement the cache",
        pass_criteria="Candidate still responds",  # noqa: S106
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
            agents=SimpleNamespace(turn=AsyncMock()),
            log=MagicMock(),
            warning=MagicMock(),
            progress=SimpleNamespace(note=lambda _block: None, declare=lambda _path: None),
        ),
    )
    turns = MultiAgentTurns(ctx, _options())
    issue_board.ensure_progress_file(turns.progress_path)
    issue_board.ensure_roadmap_file(turns.roadmap_path)
    return turns


def _search() -> HypothesisSearch:
    return HypothesisSearch(HypothesisConfig(max_rounds=2, official_eval_every=2))


def _plan_request(*, round_number: int = 1, state: HypothesisState | None = None) -> PlanRequest:
    return PlanRequest(
        round_number=round_number,
        state=state if state is not None else _search().initial(),
        records=[],
        carry=CarryOver(),
        profiler_summary=None,
        plateau_warning=None,
        provisional_candidates=0,
        profile_guidance=FocusView(),
    )


def _request() -> tuple[PlanRequest, AttemptRequest, AttemptState]:
    search = _search()
    initial = search.initial()
    plan = _plan()
    started = search.start(initial, plan, round_number=1, current_commit=None, records=[])
    hypothesis = started.hypothesis
    assert hypothesis is not None
    request = AttemptRequest(
        round_number=1,
        plan=plan,
        planned_official_reason="final_round",
        records=[],
        active_hypothesis=hypothesis,
        profile_focus=FocusView(),
        last_profile_focus="decode",
    )
    attempt = AttemptState(agent_run_state=started.state, feedback=None, retry=1)
    return _plan_request(state=initial), request, attempt


def _handle() -> SimpleNamespace:
    return SimpleNamespace(close=AsyncMock())


def test_roles_open_close_and_plan_context(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    turns.ctx.agents = SimpleNamespace(
        default_definition=lambda role: role,
        spawn=AsyncMock(side_effect=lambda role: _handle()),  # noqa: ARG005
        turn=AsyncMock(return_value=_plan()),
    )
    asyncio.run(turns.open())
    assert turns.ctx.agents.spawn.await_count == 4

    plan_request, _, _ = _request()
    context = turns._plan_context(plan_request)
    assert context.objective_location == "OBJECTIVE.md"
    planned = asyncio.run(turns.plan(plan_request))
    assert planned.hypothesis_id == "h1"
    assert (tmp_path / "progress-artifacts" / "plans" / "round-0001.json").is_file()
    call = turns.ctx.agents.turn.await_args
    assert call.kwargs["label"] == "round-1-plan"
    assert isinstance(call.kwargs["context"].objective_location, str)
    assert isinstance(call.args[0].access, ReadOnly)

    asyncio.run(turns.close())
    assert all(
        getattr(turns, name).close.await_count == 1
        for name in ("designer", "worker", "judge", "profiler")
    )


def test_plan_reprompts_reused_id_then_accepts_new_one(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    turns.designer = _handle()
    search = _search()
    existing = search.start(
        search.initial(), _plan(), round_number=1, current_commit=None, records=[]
    ).state
    request = _plan_request(round_number=2, state=existing)
    turns.ctx.agents.turn = AsyncMock(side_effect=[_plan(), _plan(hypothesis_id="h2")])
    result = asyncio.run(turns.plan(request))
    assert result.hypothesis_id == "h2"
    assert turns.ctx.agents.turn.await_count == 2
    second_call = turns.ctx.agents.turn.await_args_list[1]
    assert "rejected" in second_call.kwargs["message"]


def test_plan_rejects_duplicate_self_and_existing_hypothesis_updates(tmp_path: Path) -> None:
    turns = _turns(tmp_path)
    state = HypothesisState()
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
    existing = (
        _search().start(state, _plan(), round_number=1, current_commit=None, records=[]).state
    )
    with pytest.raises(InvalidPlanError, match="already used"):
        turns._validate_plan(_plan(), existing)


def test_pre_round_profile_and_profiler_guards(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    turns.designer = _handle()
    turns.ctx.agents.turn = AsyncMock(
        return_value=PreRoundDecision(
            need_profile=True, profile_focus="decode", reasoning="measure"
        )
    )
    decision = asyncio.run(turns.pre_round_decision(1, CarryOver(), has_history=False))
    assert decision.need_profile
    assert decision.profile_focus == "decode"

    turns.profiler = _handle()
    summary_reply = ProfilerSummary(analysis="profiled", bottlenecks="decode", suggestions="cache")
    turns.ctx.agents.turn = AsyncMock(return_value=summary_reply)
    summary = asyncio.run(turns.profile(1, "decode"))
    assert summary is not None
    assert summary.analysis == "profiled"
    call = turns.ctx.agents.turn.await_args
    assert call is not None
    assert call.kwargs["mcp_servers"] is None or isinstance(call.kwargs["mcp_servers"], list)
    assert isinstance(call.args[0].access, ReadOnly)

    turns.ctx.environment.profiler_kind = ProfilerKind.NONE
    assert asyncio.run(turns.profile(1, "decode")) is None
    turns.ctx.environment.profiler_kind = ProfilerKind.TORCH
    with pytest.raises(UnsupportedProfilerError):
        turns._profiler()


def test_implementer_marks_paid_turn_before_snapshot_and_judge_applies_pareto_guard(
    tmp_path: Path,
) -> None:
    turns = cast("Any", _turns(tmp_path))
    _, request, attempt = _request()
    response = ImplementerResponse(
        summary="cache added",
        expected_behavior="faster",
        hypothesis_outcome=HypothesisOutcome.SUPPORTED,
    )
    turns.worker = _handle()
    events: list[str] = []
    original_marker = issue_board.write_implementer_start_marker

    def marker(path: Path, round_number: int, retry: int) -> Path:
        events.append("marker")
        return original_marker(path, round_number, retry)

    async def agents_turn(
        _role: object,
        *,
        before_paid: Callable[[], Awaitable[None]] | None = None,
        **_kwargs: object,
    ) -> ImplementerResponse:
        events.append("prompt")
        if before_paid is not None:
            await before_paid()
        events.append("worker")
        return response

    turns.ctx.agents.turn = agents_turn

    with mock.patch("vibesys.loops.multi.turns.issue_board.write_implementer_start_marker", marker):
        returned, synthesized = asyncio.run(turns.implement(request, attempt))
    assert returned == response
    assert not synthesized
    assert events == ["prompt", "marker", "worker"]

    attempt.implementation = response
    turns.judge = _handle()
    turns.ctx.agents.turn = AsyncMock(
        return_value=JudgeResponse(analysis="plausible", feedback="", verdict=Verdict.PASS)
    )
    verdict = asyncio.run(turns.review(request, attempt, "conflicts with retained frontier"))
    assert verdict.verdict is Verdict.FAIL
    assert "Pareto guard" in verdict.analysis
    turns.ctx.events.emit.assert_called()


def test_implement_uses_continuation_role_when_hypothesis_has_next_step(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    turns.worker = _handle()
    _, request, attempt = _request()
    request.active_hypothesis.next_step = "finish wiring"
    response = ImplementerResponse(summary="cache added", expected_behavior="faster")
    turns.ctx.agents.turn = AsyncMock(return_value=response)
    asyncio.run(turns.implement(request, attempt))
    call = turns.ctx.agents.turn.await_args
    assert call is not None
    assert call.args[0] is MULTI_IMPLEMENTER_CONTINUATION


def test_implement_detects_synthesized_reply_by_sentinel_summary(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    turns.worker = _handle()
    _, request, attempt = _request()
    turns.ctx.agents.turn = AsyncMock(
        return_value=ImplementerResponse(
            summary="Implementer invocation timed out.",
            expected_behavior="unknown",
            hypothesis_outcome="inconclusive",
            evidence="timed out",
        )
    )
    _, synthesized = asyncio.run(turns.implement(request, attempt))
    assert synthesized


def test_missing_implementation_and_resolved_skills_on_empty_sources(tmp_path: Path) -> None:
    turns = _turns(tmp_path)
    _, request, attempt = _request()
    with pytest.raises(MissingImplementationError):
        asyncio.run(turns.review(request, attempt, None))
    assert turns._resolved_skills([]) == []


def test_judge_role_is_used_for_review(tmp_path: Path) -> None:
    turns = cast("Any", _turns(tmp_path))
    _, request, attempt = _request()
    attempt.implementation = ImplementerResponse(summary="ok", expected_behavior="faster")
    turns.judge = _handle()
    turns.ctx.agents.turn = AsyncMock(
        return_value=JudgeResponse(analysis="ok", feedback="", verdict=Verdict.PASS)
    )
    asyncio.run(turns.review(request, attempt, None))
    call = turns.ctx.agents.turn.await_args
    assert call is not None
    assert call.args[0] is MULTI_JUDGE
