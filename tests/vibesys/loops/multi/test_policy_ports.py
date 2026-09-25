"""Direct built-in strategy decisions with fake async roles and effects."""

# ruff: noqa: SLF001  # Direct policy seams are intentionally exercised.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import AttemptDecision, AttemptState
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
)
from vibesys.loops.multi.decisions import AttemptRequest, HypothesisEngine, PlanRequest
from vibesys.loops.multi.session import MultiSession
from vibesys.loops.multi.turns import MultiAgentTurns
from vibesys.loops.profile_multi.controller import HypothesisEngine as ProfileHypothesisEngine
from vibesys.loops.profile_multi.session import ProfileMultiSession
from vibesys.loops.single.session import SingleSession
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
    ValidationRecipe,
    ValidationRecipeArtifact,
    Verdict,
)
from vibesys.search.hypothesis import OrchestratorPlan

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


@dataclass
class _FakeTurns:
    calls: list[str]
    needs_profile: bool = True
    profiler_enabled: bool = True

    async def pre_round_decision(
        self, _round_number: int, _carry: CarryOver, *, has_history: bool
    ) -> PreRoundDecision:
        assert not has_history
        self.calls.append("prepass")
        return PreRoundDecision(
            need_profile=self.needs_profile, profile_focus="decode", reasoning="measure first"
        )

    async def profile(self, _round_number: int, focus: str) -> ProfilerSummary | None:
        if not self.profiler_enabled:
            return None
        self.calls.append(f"profile:{focus}")
        return ProfilerSummary(analysis="analysis", bottlenecks="decode", suggestions="cache")

    async def implement(
        self, _request: AttemptRequest, state: AttemptState
    ) -> tuple[ImplementerResponse, bool]:
        self.calls.append(f"implement:{state.retry}")
        return ImplementerResponse(summary="changed", expected_behavior="faster"), False

    async def review(
        self, _request: AttemptRequest, state: AttemptState, _conflict: str | None
    ) -> JudgeResponse:
        self.calls.append(f"judge:{state.retry}")
        return JudgeResponse(analysis="reviewed", feedback="", verdict=Verdict.PASS)


def _attempt() -> tuple[AttemptRequest, AttemptState]:
    plan = OrchestratorPlan(
        hypothesis_id="h1",
        hypothesis="cache decode",
        task="implement cache",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="measure decode",
    )
    engine = HypothesisEngine.create(AgentRunState()).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    return (
        AttemptRequest(
            round_number=1,
            plan=plan,
            planned_official_reason="final_round",
            records=[],
            active_hypothesis=hypothesis,
            engine=engine,
            last_profile_focus="decode",
        ),
        AttemptState(agent_run_state=engine.state, feedback=None),
    )


def test_multi_prepass_profiles_only_when_requested_and_enabled() -> None:
    calls: list[str] = []
    turns = _FakeTurns(calls)
    session = cast("Any", MultiSession.__new__(MultiSession))
    session.turns = turns
    session.round_number = 1
    session.records = []
    session.carry = CarryOver()

    assert asyncio.run(session._pre_round_profile()) is not None
    assert calls == ["prepass", "profile:decode"]

    calls.clear()
    turns.needs_profile = False
    assert asyncio.run(session._pre_round_profile()) is None
    assert calls == ["prepass"]

    calls.clear()
    turns.needs_profile = True
    turns.profiler_enabled = False
    assert asyncio.run(session._pre_round_profile()) is None
    assert calls == ["prepass"]


def test_profile_guidance_prepares_cursor_before_designer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibesys.evaluators.input_manifest import ProfileGuidedInput  # noqa: PLC0415

    calls: list[str] = []
    config = ProfileGuidedInput(command=("fake-profiler",))
    session = cast("Any", ProfileMultiSession.__new__(ProfileMultiSession))
    from vibesys.loops.profile_multi.session import _ProfilePolicy  # noqa: PLC0415

    session.profile = _ProfilePolicy(config)
    session.ctx = SimpleNamespace(events=SimpleNamespace(emit=lambda *_args, **_kwargs: None))
    session.state = AgentRunState()
    session.engine = ProfileHypothesisEngine.create(session.state, config=config)
    session.records = []
    session.carry = CarryOver()
    session.round_number = 1
    session.last_profile_focus = "decode"
    session.workspace = SimpleNamespace(revision="a" * 40)
    session.options = SimpleNamespace(max_rounds=1, official_eval_every=1)

    async def fake_attribution(
        _ctx: object, _config: ProfileGuidedInput, *, round_number: int
    ) -> tuple[()]:
        calls.append(f"profile-prepared:{round_number}")
        return ()

    async def fake_plan(_request: object) -> OrchestratorPlan:
        calls.append("designer")
        return OrchestratorPlan(
            hypothesis_id="h1",
            task="implement cache",
            pass_criteria="tests pass",  # noqa: S106
            reasoning="plan",
        )

    async def fake_save(_state: AgentRunState, *, label: str) -> None:
        calls.append(f"checkpoint:{label}")

    monkeypatch.setattr("vibesys.loops.profile_multi.session.run_attribution", fake_attribution)
    session.turns = SimpleNamespace(plan=fake_plan)
    session._save_state = fake_save
    session._pre_round_profile = AsyncMock(return_value=None)
    session._apply_rollback = AsyncMock()

    asyncio.run(session.select_hypothesis())
    assert calls.index("profile-prepared:1") < calls.index("designer")
    assert any(call.startswith("checkpoint:profile-guided: prepare round 1") for call in calls)
    assert calls.index("designer") > next(
        index for index, call in enumerate(calls) if call.startswith("checkpoint:")
    )


def test_multi_validation_failure_checkpoints_before_retry_and_official_gate() -> None:
    calls: list[str] = []
    request, state = _attempt()
    selected = SimpleNamespace(request=request, attempt=state)
    session = cast("Any", MultiSession.__new__(MultiSession))
    session.turns = _FakeTurns(calls)
    session.records = []
    session.round_number = 1
    session.options = SimpleNamespace(max_rounds=1, judge_every=1, official_eval_every=1)
    session.workspace = SimpleNamespace(revision="a" * 40)
    session.ctx = SimpleNamespace(log=lambda _message: calls.append("log"))
    validation_feedback = ["bad recipe", None]

    async def validate(_selected: object, _recipe: str | None) -> str | None:
        calls.append(f"validate:{state.retry}")
        return validation_feedback.pop(0)

    async def checkpoint(_selected: object) -> None:
        calls.append(f"checkpoint:{state.retry}")

    async def gates(
        _retry: int, _commit: str | None, *, reuse_accuracy: bool
    ) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
        assert not reuse_accuracy
        calls.append(f"official-gate:{state.retry}")
        return None, FrameworkBenchmarkOutcome(), True

    session._validate_local = validate
    session._checkpoint_active = checkpoint
    session._run_gates = gates
    session._record_official_decision = lambda _selected, *, run, reason: calls.append(
        f"official-decision:{state.retry}:{run}:{reason}"
    )

    async def run_attempts() -> None:
        for retry in (1, 2):
            state.retry = retry
            if not await session.implement(selected):
                continue
            decision = await session.review(selected)
            if decision is AttemptDecision.OFFICIAL and await session.official_gates(selected):
                break

    asyncio.run(run_attempts())
    assert state.passed
    assert calls.index("checkpoint:1") < calls.index("implement:2")
    assert [call for call in calls if call.startswith(("implement:", "judge:", "validate:"))] == [
        "implement:1",
        "judge:1",
        "validate:1",
        "implement:2",
        "judge:2",
        "validate:2",
    ]
    assert calls.index("official-decision:2:True:final_round") < calls.index("official-gate:2")


def test_single_uses_only_combined_turn_and_its_own_verdict() -> None:
    calls: list[str] = []
    request, state = _attempt()
    state.retry = 1
    selected = SimpleNamespace(request=request, attempt=state)
    session = cast("Any", SingleSession.__new__(SingleSession))
    session.options = SimpleNamespace(max_rounds=1, official_eval_every=1)
    session.records = []
    session.round_number = 1

    async def combined(_request: AttemptRequest, _state: AttemptState) -> SingleAgentRoundResponse:
        calls.append("combined:1")
        return SingleAgentRoundResponse(
            summary="changed",
            expected_behavior="faster",
            self_review="checked",
            feedback="",
            verdict=Verdict.PASS,
            bottlenecks="decode",
            suggestions="cache",
            profile_analysis="measured",
        )

    session.turns = SimpleNamespace(combined=combined)
    decision = asyncio.run(session.combined_turn(selected))
    assert decision is AttemptDecision.OFFICIAL
    assert calls == ["combined:1"]


def test_multi_designer_corrects_reused_hypothesis_id_before_persisting(
    tmp_path: Path,
) -> None:
    state = (
        HypothesisEngine.create(AgentRunState())
        .start(
            OrchestratorPlan(
                hypothesis_id="used",
                task="first task",
                pass_criteria="tests pass",  # noqa: S106
                reasoning="first plan",
            ),
            started_round=1,
        )
        .state
    )
    request = PlanRequest(
        round_number=2,
        state=state,
        records=[],
        carry=CarryOver(),
        profiler_summary=None,
        plateau_warning=None,
        provisional_candidates=0,
        profile_guidance=HypothesisEngine.create(state).guidance,
    )
    turns = cast("Any", MultiAgentTurns.__new__(MultiAgentTurns))
    turns.progress_path = tmp_path / "progress.md"
    issue_board.ensure_progress_file(turns.progress_path)
    calls: list[str] = []
    turns.ctx = SimpleNamespace(log=calls.append)
    turns._plan_prompt = lambda _request: "plan prompt"
    turns._skills = lambda selections: (selections, [])
    plans = iter(
        [
            OrchestratorPlan(
                hypothesis_id="used",
                task="duplicate task",
                pass_criteria="tests pass",  # noqa: S106
                reasoning="retry",
            ),
            OrchestratorPlan(
                hypothesis_id="fresh",
                task="new task",
                pass_criteria="tests pass",  # noqa: S106
                reasoning="corrected",
            ),
        ]
    )

    async def designer(_prompt: str, message: str, _label: str) -> OrchestratorPlan:
        calls.append(message)
        return next(plans)

    turns._designer_turn = designer
    result = asyncio.run(turns.plan(request))
    assert result.hypothesis_id == "fresh"
    assert "previous plan was rejected" in calls[-1]
    assert "fresh" in turns.progress_path.read_text()


def test_multi_read_only_role_restores_unauthorized_edits() -> None:
    calls: list[object] = []
    pending = ["roadmap/index.md", "src/server.py"]

    class Workspace:
        async def snapshot(self, label: str) -> str:
            calls.append(label)
            return "baseline"

        async def pending_changes(self) -> list[str]:
            return pending.copy()

        async def restore(
            self, revision: str, *, clean: bool, preserve_paths: tuple[str, ...]
        ) -> None:
            calls.append((revision, clean, preserve_paths))
            pending.remove("src/server.py")

    agent = SimpleNamespace(
        turn_structured=AsyncMock(
            return_value=PreRoundDecision(need_profile=False, reasoning="enough evidence")
        )
    )
    turns = cast("Any", MultiAgentTurns.__new__(MultiAgentTurns))
    turns.workspace = Workspace()
    turns.ctx = SimpleNamespace(log=calls.append)
    result = asyncio.run(
        turns._read_only(
            agent,
            message="decide",
            prompt="read only",
            response_cls=PreRoundDecision,
            fallback_factory=lambda: PreRoundDecision(need_profile=False, reasoning="fallback"),
            label="prepass",
            allowed=("roadmap/index.md",),
        )
    )
    assert isinstance(result, PreRoundDecision)
    assert ("baseline", True, ("roadmap/index.md",)) in calls
    assert pending == ["roadmap/index.md"]


def test_multi_implementer_marks_paid_turn_before_invocation(tmp_path: Path) -> None:
    request, state = _attempt()
    state.retry = 1
    calls: list[str] = []

    async def snapshot(label: str) -> str:
        calls.append(label)
        return "revision"

    async def implement(_message: str, **_kwargs: object) -> ImplementerResponse:
        calls.append("paid-turn")
        assert issue_board.next_implementer_attempt(tmp_path / "progress.md", 1) == 2
        return ImplementerResponse(summary="cache added", expected_behavior="faster")

    turns = cast("Any", MultiAgentTurns.__new__(MultiAgentTurns))
    turns.progress_path = tmp_path / "progress.md"
    issue_board.ensure_progress_file(turns.progress_path)
    turns.workspace = SimpleNamespace(snapshot=snapshot)
    turns.worker = SimpleNamespace(turn_structured=implement)
    turns._skills = lambda selections: (selections, [])
    turns._implementer_prompt = lambda *_args: "implement prompt"
    response, synthesized = asyncio.run(turns.implement(request, state))
    assert response.summary == "cache added"
    assert not synthesized
    assert calls == [
        "round-1-retry-1-paid-marker",
        "paid-turn",
        "round-1-retry-1-implementer",
    ]


def test_multi_local_validation_restores_mutated_candidate(tmp_path: Path) -> None:
    source = tmp_path / "server.py"
    source.write_text("VALUE = 1\n")
    recipe = ValidationRecipe(
        name="focused-tests",
        command="python -m pytest tests/test_server.py",
        input_paths=["server.py"],
        purpose="Check the server contract.",
    )
    (tmp_path / "recipes.json").write_text(
        ValidationRecipeArtifact(recipes=[recipe]).model_dump_json()
    )
    progress_path = tmp_path / "progress.md"
    issue_board.ensure_progress_file(progress_path)
    calls: list[str] = []

    async def snapshot(label: str) -> str:
        calls.append(label)
        return "baseline"

    async def execute(_command: str, *, timeout_seconds: int) -> SimpleNamespace:
        assert timeout_seconds == recipe.timeout_seconds
        calls.append("execute")
        source.write_text("VALUE = 2\n")
        return SimpleNamespace(exit_code=0, output="pass")

    async def pending_changes() -> list[str]:
        return ["server.py"] if source.read_text() == "VALUE = 2\n" else []

    async def restore(_revision: str, *, clean: bool) -> None:
        assert clean
        calls.append("restore")
        source.write_text("VALUE = 1\n")

    session = cast("Any", MultiSession.__new__(MultiSession))
    session.round_number = 1
    session.turns = SimpleNamespace(progress_path=progress_path)
    session.workspace = SimpleNamespace(
        path=tmp_path, snapshot=snapshot, pending_changes=pending_changes, restore=restore
    )
    session.ctx = SimpleNamespace(environment=SimpleNamespace(execute=execute))
    selected = SimpleNamespace(attempt=SimpleNamespace(retry=1))
    feedback = asyncio.run(session._validate_local(selected, "recipes.json"))
    assert feedback is not None
    assert "mutated the workspace" in feedback
    assert source.read_text() == "VALUE = 1\n"
    assert calls.index("execute") < calls.index("restore")
    assert "framework-validation" in calls[-1]


def test_multi_official_gate_failure_persists_revalidation_for_exact_commit() -> None:
    request, attempt = _attempt()
    attempt.retry = 1
    attempt.official_reason = "final_round"
    selected = SimpleNamespace(request=request, attempt=attempt)
    session = cast("Any", MultiSession.__new__(MultiSession))
    session.workspace = SimpleNamespace(revision="a" * 40)
    decisions: list[tuple[bool, str]] = []
    session._record_official_decision = lambda _selected, *, run, reason: decisions.append(
        (run, reason)
    )
    session._checkpoint_active = AsyncMock()
    session._run_gates = AsyncMock(
        side_effect=[
            ("benchmark failed", FrameworkBenchmarkOutcome(), True),
            (None, FrameworkBenchmarkOutcome(), True),
        ]
    )

    assert not asyncio.run(session._official_gates(selected))
    hypothesis = request.active_hypothesis
    assert attempt.feedback == "benchmark failed"
    assert hypothesis.gate_revalidation_pending
    assert hypothesis.gate_accuracy_passed
    assert hypothesis.gate_candidate_commit == "a" * 40
    session._checkpoint_active.assert_awaited_once_with(selected)

    assert asyncio.run(session._official_gates(selected))
    assert attempt.passed
    assert session._run_gates.await_args_list[1].kwargs["reuse_accuracy"]
    assert decisions == [(True, "final_round"), (True, "final_round")]


def test_multi_run_gates_stops_after_resource_reconciliation_failure() -> None:
    session = cast("Any", MultiSession.__new__(MultiSession))
    reconcile = AsyncMock(return_value="model request rejected")
    session.ctx = SimpleNamespace(environment=SimpleNamespace(reconcile_model_requests=reconcile))
    session.turns = SimpleNamespace(worker=SimpleNamespace(backend_name="cli"))
    session._accuracy_gate = AsyncMock()
    session._benchmark_gate = AsyncMock()

    feedback, benchmark, accuracy_passed = asyncio.run(
        session._run_gates(1, "a" * 40, reuse_accuracy=False)
    )
    assert feedback == "model request rejected"
    assert benchmark.metric_value is None
    assert not accuracy_passed
    session._accuracy_gate.assert_not_awaited()
    session._benchmark_gate.assert_not_awaited()


def test_multi_run_gates_orders_accuracy_before_benchmark() -> None:
    session = cast("Any", MultiSession.__new__(MultiSession))
    session.ctx = SimpleNamespace(
        environment=SimpleNamespace(reconcile_model_requests=AsyncMock(return_value=None))
    )
    session.turns = SimpleNamespace(worker=SimpleNamespace(backend_name="cli"))
    session._accuracy_gate = AsyncMock(
        side_effect=[
            AccuracyGateResult(
                command="check",
                passed=False,
                output="failed",
                feedback="accuracy rejected",
                executed=True,
            ),
            AccuracyGateResult(
                command="check", passed=True, output="passed", feedback=None, executed=True
            ),
        ]
    )
    outcome = FrameworkBenchmarkOutcome(metric_value=12.0)
    session._benchmark_gate = AsyncMock(
        return_value=BenchmarkGateResult(
            command="measure", output="passed", executed=True, outcome=outcome
        )
    )

    feedback, _, passed = asyncio.run(session._run_gates(1, "a" * 40, reuse_accuracy=False))
    assert feedback == "accuracy rejected"
    assert not passed
    session._benchmark_gate.assert_not_awaited()

    feedback, benchmark, passed = asyncio.run(session._run_gates(2, "b" * 40, reuse_accuracy=True))
    assert feedback is None
    assert benchmark is outcome
    assert passed
    assert session._accuracy_gate.await_args_list[1].kwargs["reuse"]
