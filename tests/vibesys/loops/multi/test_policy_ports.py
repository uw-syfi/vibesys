"""Direct built-in strategy decisions with fake async roles and effects."""

# ruff: noqa: SLF001  # Direct policy seams are intentionally exercised.

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import AttemptDecision, AttemptState
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.loops.multi.decisions import (
    STATIC_GUIDANCE,
    AttemptRequest,
    PlainGuidance,
    PlanRequest,
)
from vibesys.loops.multi.session import MultiSession
from vibesys.loops.multi.turns import MultiAgentTurns
from vibesys.loops.profile_multi.controller import HypothesisEngine as ProfileHypothesisEngine
from vibesys.loops.profile_multi.session import ProfileMultiSession
from vibesys.loops.single.session import SingleSession
from vibesys.orchestration.runtime import GateRunResult
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
    ValidationRecipe,
    ValidationRecipeArtifact,
    Verdict,
)
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch
from vibesys.search.hypothesis.transitions import CarryOver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    import pytest


@dataclass
class _FakeTurns:
    calls: list[str]
    needs_profile: bool = True
    profiler_enabled: bool = True
    worker: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(backend_name="cli"))

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


def _search() -> HypothesisSearch:
    return HypothesisSearch(HypothesisConfig(max_rounds=1))


def _attempt() -> tuple[AttemptRequest, AttemptState]:
    plan = OrchestratorPlan(
        hypothesis_id="h1",
        hypothesis="cache decode",
        task="implement cache",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="measure decode",
    )
    search = _search()
    started = search.start(search.initial(), plan, round_number=1, current_commit=None, records=[])
    hypothesis = started.hypothesis
    assert hypothesis is not None
    return (
        AttemptRequest(
            round_number=1,
            plan=plan,
            planned_official_reason="final_round",
            records=[],
            active_hypothesis=hypothesis,
            engine=STATIC_GUIDANCE,
            last_profile_focus="decode",
        ),
        AttemptState(agent_run_state=started.state, feedback=None),
    )


def test_multi_prepass_profiles_only_when_requested_and_enabled() -> None:
    calls: list[str] = []
    turns = _FakeTurns(calls)
    session = cast("Any", MultiSession.__new__(MultiSession))
    session.turns = turns
    session.round_number = 1
    session.state = AgentRunState()
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

    async def fake_commit(*, sequence: int, writes: object, **kwargs: object) -> None:
        del sequence, writes
        calls.append(f"checkpoint:{kwargs['label']}")

    session.ctx = SimpleNamespace(
        events=SimpleNamespace(emit=lambda *_args, **_kwargs: None),
        state=SimpleNamespace(commit=fake_commit),
    )
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

    monkeypatch.setattr("vibesys.loops.profile_multi.session.run_attribution", fake_attribution)
    session.turns = SimpleNamespace(plan=fake_plan)
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
    session.search = _search()
    session.turns = _FakeTurns(calls)
    session.round_number = 1
    session.options = SimpleNamespace(max_rounds=1, judge_every=1, official_eval_every=1)
    session.workspace = SimpleNamespace(revision="a" * 40)
    session.state = state.agent_run_state
    session._gate_recorder = None
    validation_feedback = ["bad recipe", None]

    async def validate(_selected: object, _recipe: str | None) -> str | None:
        calls.append(f"validate:{state.retry}")
        return validation_feedback.pop(0)

    async def commit(*, sequence: int, writes: object, **kwargs: object) -> None:
        del sequence, writes, kwargs
        calls.append(f"checkpoint:{state.retry}")

    session.ctx = SimpleNamespace(
        log=lambda _message: calls.append("log"),
        state=SimpleNamespace(commit=commit),
    )

    async def gates(*, reuse_accuracy: bool, **_kwargs: object) -> GateRunResult:
        assert not reuse_accuracy
        calls.append(f"official-gate:{state.retry}")
        return GateRunResult(
            feedback=None, benchmark=FrameworkBenchmarkOutcome(), accuracy_passed=True
        )

    session.ctx.gates = SimpleNamespace(run=gates)
    session._validate_local = validate
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
    search = _search()
    state = search.start(
        search.initial(),
        OrchestratorPlan(
            hypothesis_id="used",
            task="first task",
            pass_criteria="tests pass",  # noqa: S106
            reasoning="first plan",
        ),
        round_number=1,
        current_commit=None,
        records=[],
    ).state
    request = PlanRequest(
        round_number=2,
        state=state,
        records=[],
        carry=CarryOver(),
        profiler_summary=None,
        plateau_warning=None,
        provisional_candidates=0,
        profile_guidance=PlainGuidance(),
    )
    turns = cast("Any", MultiAgentTurns.__new__(MultiAgentTurns))
    turns.progress_path = tmp_path / "progress.md"
    turns.roadmap_location = "roadmap.md"
    turns._plan_context = lambda _request: {}
    issue_board.ensure_progress_file(turns.progress_path)
    calls: list[str] = []
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

    async def agents_turn(_role: object, **kwargs: object) -> OrchestratorPlan:
        message = kwargs.get("message")
        if message is not None:
            calls.append(cast("str", message))
        return next(plans)

    turns.ctx = SimpleNamespace(log=calls.append, agents=SimpleNamespace(turn=agents_turn))
    turns.designer = SimpleNamespace()
    result = asyncio.run(turns.plan(request))
    assert result.hypothesis_id == "fresh"
    assert "previous plan was rejected" in calls[-1]
    assert "fresh" in turns.progress_path.read_text()


# Role-isolation restoration (unauthorized-edit revert) is now host code:
# ctx.agents.turn owns it for every strategy, covered by
# tests/vibesys/orchestration/test_agents_turn.py. Multi's turns.py no
# longer has a ``_read_only`` wrapper of its own to test here.


def test_multi_implementer_marks_paid_turn_before_invocation(tmp_path: Path) -> None:
    """``before_paid`` (the paid-work marker hook ``ctx.agents.turn`` runs
    right before its pre-turn snapshot) must fire before the implementer
    turn itself, so a crash mid-turn still resumes from a committed marker.
    """
    request, state = _attempt()
    state.retry = 1
    calls: list[str] = []

    async def agents_turn(
        _role: object,
        *,
        before_paid: Callable[[], Awaitable[None]] | None = None,
        **_kwargs: object,
    ) -> ImplementerResponse:
        assert before_paid is not None
        await before_paid()
        assert issue_board.next_implementer_attempt(tmp_path / "progress.md", 1) == 2
        calls.append("paid-turn")
        return ImplementerResponse(summary="cache added", expected_behavior="faster")

    turns = cast("Any", MultiAgentTurns.__new__(MultiAgentTurns))
    turns.progress_path = tmp_path / "progress.md"
    issue_board.ensure_progress_file(turns.progress_path)
    turns.ctx = SimpleNamespace(agents=SimpleNamespace(turn=agents_turn))
    turns.worker = SimpleNamespace()
    turns._implementer_context = lambda *_args: {}
    response, synthesized = asyncio.run(turns.implement(request, state))
    assert response.summary == "cache added"
    assert not synthesized
    assert calls == ["paid-turn"]


def _fake_transaction_factory(
    snapshot: Callable[[str], Awaitable[str]], restore: Callable[..., Awaitable[None]]
) -> Callable[..., AbstractAsyncContextManager[Any]]:
    """Match ``WorkspaceHandle.transaction``'s snapshot/restore-on-exit semantics
    against a fake workspace's own ``snapshot``/``restore``.
    """

    @asynccontextmanager
    async def transaction(
        *, preserve: tuple[str, ...] = (), label: str = "tx"
    ) -> AsyncIterator[Any]:
        revision = await snapshot(label)
        committed = False

        def commit() -> None:
            nonlocal committed
            committed = True

        tx = SimpleNamespace(commit=commit)
        try:
            yield tx
        except BaseException:
            if not committed:
                await restore(revision, clean=True, preserve_paths=preserve)
            raise
        else:
            if not committed:
                await restore(revision, clean=True, preserve_paths=preserve)

    return transaction


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

    async def restore(_revision: str, *, clean: bool, preserve_paths: tuple[str, ...] = ()) -> None:
        del preserve_paths
        assert clean
        calls.append("restore")
        source.write_text("VALUE = 1\n")

    session = cast("Any", MultiSession.__new__(MultiSession))
    session.round_number = 1
    session.turns = SimpleNamespace(progress_path=progress_path)
    session.workspace = SimpleNamespace(
        path=tmp_path,
        snapshot=snapshot,
        pending_changes=pending_changes,
        restore=restore,
        transaction=_fake_transaction_factory(snapshot, restore),
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
    """`_official_gates` policy: revalidation bookkeeping and gate reuse on retry.

    `ctx.gates.run`'s own mechanics (resource reconciliation, accuracy-then-
    benchmark ordering) are host mechanics, covered at the host level in
    `tests/vibesys/api/test_gates_run.py`.
    """
    request, attempt = _attempt()
    attempt.retry = 1
    attempt.official_reason = "final_round"
    selected = SimpleNamespace(request=request, attempt=attempt)
    session = cast("Any", MultiSession.__new__(MultiSession))
    session.workspace = SimpleNamespace(revision="a" * 40)
    session.turns = SimpleNamespace(worker=SimpleNamespace(backend_name="cli"))
    session.state = attempt.agent_run_state
    session._gate_recorder = None
    session.round_number = 1
    decisions: list[tuple[bool, str]] = []
    session._record_official_decision = lambda _selected, *, run, reason: decisions.append(
        (run, reason)
    )
    gates_run = AsyncMock(
        side_effect=[
            GateRunResult(
                feedback="benchmark failed",
                benchmark=FrameworkBenchmarkOutcome(),
                accuracy_passed=True,
            ),
            GateRunResult(
                feedback=None, benchmark=FrameworkBenchmarkOutcome(), accuracy_passed=True
            ),
        ]
    )
    session.ctx = SimpleNamespace(
        state=SimpleNamespace(commit=AsyncMock()), gates=SimpleNamespace(run=gates_run)
    )

    assert not asyncio.run(session._official_gates(selected))
    hypothesis = request.active_hypothesis
    assert attempt.feedback == "benchmark failed"
    assert hypothesis.gate_revalidation_pending
    assert hypothesis.gate_accuracy_passed
    assert hypothesis.gate_candidate_commit == "a" * 40
    session.ctx.state.commit.assert_awaited_once()

    assert asyncio.run(session._official_gates(selected))
    assert attempt.passed
    assert gates_run.await_args_list[1].kwargs["reuse_accuracy"]
    assert decisions == [(True, "final_round"), (True, "final_round")]
