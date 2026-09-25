"""Profile single strategy plan correction and combined role handoff.

``turns.py`` renders through ``ctx.agents.turn`` (see
``vibesys.orchestration.agents``), which owns the turn mechanics
(rendering, isolation, timeout->fallback, correction retries) generically
for every strategy; those mechanics have their own tests in
``tests/vibesys/orchestration/test_agents_turn.py``. These tests cover only
what's specific to ``profile_single``: the context dict each role renders
from, the state-dependent plan-ID retry that wraps ``ctx.agents.turn`` (not
expressible as ``Role.check``), and the board writes around each turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from vibesys.agent_run import issue_board
from vibesys.constants import DomainName
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.loops.agent_options import AgentOrchestrationOptions
from vibesys.loops.profile_single.session import AttemptRequest, PlanRequest
from vibesys.loops.profile_single.turns import InvalidPlanError, ProfileSingleTurns
from vibesys.profilers import ProfilerKind
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import PROFILE_SINGLE_COMBINED, SingleAgentRoundResponse
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.hypothesis.attempts import AttemptState
from vibesys.search.hypothesis.state import HypothesisState
from vibesys.search.hypothesis.transitions import CarryOver, start_hypothesis
from vibesys.search.profile_focus import FocusView

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


def _configured_turns(tmp_path: Path) -> ProfileSingleTurns:
    view = SimpleNamespace(
        paths=SimpleNamespace(
            objective="OBJECTIVE.md", benchmark_command=None, accuracy_command=None
        ),
        prompt_notes="Run locally",
        profile_execution="local",
    )
    context = SimpleNamespace(
        agents=SimpleNamespace(turn=AsyncMock()),
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
    state = start_hypothesis(HypothesisState(), plan, started_round=1)
    hypothesis = state.active_hypothesis
    assert hypothesis is not None
    plan_request = PlanRequest(1, state, [], CarryOver(), None, None, 1, FocusView())

    designer_context = turns._plan_context(plan_request)  # noqa: SLF001
    combined_context = turns._combined_context(  # noqa: SLF001
        AttemptRequest(1, plan, "profile-guided component measurement", [], hypothesis, "decode"),
        AttemptState(agent_run_state=state, feedback=None, retry=1),
    )

    assert turns.template_dir.name == "profile_single"
    assert designer_context.objective_location == "OBJECTIVE.md"
    assert designer_context.runtime_notes == "Run locally"
    assert combined_context.objective_location == "OBJECTIVE.md"
    assert combined_context.official_evaluation_reason == "profile-guided component measurement"
    assert (tmp_path / "progress-artifacts" / "plans" / "round-0001.json").exists()


@pytest.mark.asyncio
async def test_plan_reprompts_reused_hypothesis_and_records_corrected_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = start_hypothesis(HypothesisState(), _plan("used"), started_round=1)
    turns = ProfileSingleTurns.__new__(ProfileSingleTurns)
    log: list[str] = []
    agent_turn = AsyncMock(side_effect=[_plan("used"), _plan("new")])
    monkeypatch.setattr(
        turns,
        "ctx",
        SimpleNamespace(log=log.append, agents=SimpleNamespace(turn=agent_turn)),
        raising=False,
    )
    monkeypatch.setattr(turns, "designer", SimpleNamespace(), raising=False)
    monkeypatch.setattr(turns, "progress_path", tmp_path / "progress.md", raising=False)
    monkeypatch.setattr(turns, "roadmap_location", "progress-artifacts/roadmap", raising=False)
    monkeypatch.setattr(turns, "_plan_context", lambda _request: {"plan": "context"})
    monkeypatch.setattr(turns, "_skills", lambda selections: (selections, []))
    request = PlanRequest(2, state, state.rounds, CarryOver(), None, None, 0, FocusView())

    plan = await turns.plan(request)

    assert plan.hypothesis_id == "new"
    assert agent_turn.await_count == 2
    assert "previous plan was rejected" in agent_turn.await_args_list[1].kwargs["message"]
    assert "rejected" in log[0]
    assert (tmp_path / "progress-artifacts" / "plans" / "round-0002.json").exists()


@pytest.mark.asyncio
async def test_combined_turn_records_response_and_uses_hypothesis_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan("h1")
    state = start_hypothesis(HypothesisState(), plan, started_round=1)
    hypothesis = state.active_hypothesis
    assert hypothesis is not None
    turns = ProfileSingleTurns.__new__(ProfileSingleTurns)
    agent_turn = AsyncMock(return_value=_response())
    monkeypatch.setattr(
        turns, "ctx", SimpleNamespace(agents=SimpleNamespace(turn=agent_turn)), raising=False
    )
    monkeypatch.setattr(turns, "worker", SimpleNamespace(), raising=False)
    monkeypatch.setattr(turns, "progress_path", tmp_path / "progress.md", raising=False)
    monkeypatch.setattr(turns, "_skills", lambda selections: (selections, []))
    monkeypatch.setattr(turns, "_combined_context", lambda *_args: {"combined": "context"})
    request = AttemptRequest(1, plan, None, [], hypothesis, "decode")
    attempt = AttemptState(agent_run_state=state, feedback=None, retry=1)

    response = await turns.combined(request, attempt)

    assert response.verdict is Verdict.PASS
    call = agent_turn.await_args
    assert call is not None
    assert call.args[0] is PROFILE_SINGLE_COMBINED
    assert call.kwargs["context"] == {"combined": "context"}
    assert call.kwargs["session_key"] == "h1"
    assert call.kwargs["label"] == "round-1-retry-1-single-agent"
    assert "Implemented batching" in (tmp_path / "progress.md").read_text()

    # `before_paid` (run by `ctx.agents.turn` right before its pre-turn
    # snapshot, so it is git-committed before the paid call starts) writes
    # the same implementer-start marker `begin_attempt` used to write
    # directly; see the shared design brief, item 3.
    before_paid = call.kwargs["before_paid"]
    assert issue_board.next_implementer_attempt(tmp_path / "progress.md", 1) == 1
    await before_paid()
    assert issue_board.next_implementer_attempt(tmp_path / "progress.md", 1) == 2


def test_validation_rejects_reused_id() -> None:
    plan = _plan("used")
    state = start_hypothesis(HypothesisState(), plan, started_round=1)
    turns = ProfileSingleTurns.__new__(ProfileSingleTurns)

    with pytest.raises(InvalidPlanError, match="already used"):
        turns._validate_plan(_plan("used"), state)  # noqa: SLF001
