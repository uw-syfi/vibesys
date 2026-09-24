"""Direct built-in strategy decisions with fake async roles and effects."""

# ruff: noqa: SLF001  # Direct policy seams are intentionally exercised.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from vibesys.agent_run.attempts import AttemptDecision, AttemptState
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.loops.multi.decisions import AttemptRequest, HypothesisEngine
from vibesys.loops.multi.session import MultiSession
from vibesys.loops.profile_multi.controller import HypothesisEngine as ProfileHypothesisEngine
from vibesys.loops.profile_multi.session import ProfileMultiSession
from vibesys.loops.single.session import SingleSession
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
    Verdict,
)

if TYPE_CHECKING:
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
