"""Profile single policy decisions over completed round evidence."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import AttemptDecision, AttemptState, JudgeReviewed
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.options import AgentOrchestrationOptions
from vibesys.agent_run.state import AgentRunState, ProfileBottleneck
from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.loops.profile_single import session as profile_session
from vibesys.loops.profile_single.hypothesis import HypothesisEngine
from vibesys.loops.profile_single.session import (
    AttemptRequest,
    ProfileSingleRound,
    ProfileSingleSession,
    ProfileSingleSessionError,
    RoundSelection,
)
from vibesys.orchestration.runtime import GateRunResult
from vibesys.schemas import OrchestratorPlan, SingleAgentRoundResponse, Verdict
from vs_loop_state.api import RoundRecord

if TYPE_CHECKING:
    from pathlib import Path


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="h1",
        hypothesis="cache decode",
        task="implement cache",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="measure decode",
    )


def _session() -> tuple[ProfileSingleSession, ProfileSingleRound]:
    plan = _plan()
    engine = HypothesisEngine.create(
        AgentRunState(), config=ProfileGuidedInput(command=("true",))
    ).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    session = ProfileSingleSession.__new__(ProfileSingleSession)
    session.engine = engine
    session.options = AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=3,
        max_retries_per_round=3,
        judge_every=1,
        official_eval_every=2,
        memory_layout="files",
        profile_guided=ProfileGuidedInput(command=("true",)),
    )
    selection = RoundSelection(engine.state, hypothesis, plan, None)
    request = AttemptRequest(1, plan, None, [], hypothesis, "latency")
    attempt = AttemptState(agent_run_state=engine.state, feedback=None)
    return session, ProfileSingleRound(selection, request, attempt)


def test_review_failure_retains_bounded_claim_and_sets_exhaustion_carry() -> None:
    session, selected = _session()
    selected.attempt.feedback = "fix validation"
    record = RoundRecord(
        round_number=1,
        commit=None,
        perf_metric=None,
        perf_unit=None,
        hypothesis_id="h1",
        passed=False,
        judge_verdict="fail",
        hypothesis_outcome="rejected",
    )

    engine, carry, exhaustion = session._complete_policy_round(selected, record)  # noqa: SLF001

    active = engine.state.active_hypothesis
    assert active is not None
    assert active.feedback == "fix validation"
    assert active.continuation_rounds == 1
    assert exhaustion == "fix validation"
    assert "fix validation" in (carry.exhaustion_info or "")


def test_terminal_success_releases_claim_and_reports_discarded_official_candidate() -> None:
    session, selected = _session()
    selected.attempt.passed = True
    record = RoundRecord(
        round_number=1,
        commit=None,
        hypothesis_id="h1",
        passed=True,
        judge_verdict="pass",
        official_evaluation=True,
        candidate_retained=False,
        perf_metric=12.0,
        perf_unit="ops",
    )

    engine, carry, exhaustion = session._complete_policy_round(selected, record)  # noqa: SLF001

    assert engine.state.active_hypothesis is None
    assert exhaustion is None
    assert "not retained: 12.0 ops" in (carry.regression_info or "")


def _response(verdict: Verdict) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="candidate edited",
        expected_behavior="faster decode",
        self_review="checked behavior",
        feedback="repair accuracy" if verdict is Verdict.FAIL else "",
        verdict=verdict,
        bottlenecks="decode",
        suggestions="batch",
        profile_analysis="decode dominates",
    )


@pytest.mark.asyncio
async def test_profile_attribution_is_checkpointed_before_plan_and_continuation_skips_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AgentRunState()
    config = ProfileGuidedInput(command=("profile",))
    session = ProfileSingleSession.__new__(ProfileSingleSession)
    session.options = _session()[0].options
    session.state = state
    session.engine = HypothesisEngine.create(state, config=config)
    session.records = []
    session.round_number = 1
    session.carry = CarryOver()
    session.last_response = None
    session.last_profile_focus = "decode"
    monkeypatch.setattr(session, "profile", profile_session._ProfilePolicy(config), raising=False)  # noqa: SLF001
    plan = AsyncMock(return_value=_plan())
    monkeypatch.setattr(
        session,
        "turns",
        SimpleNamespace(plan=plan, progress_path=tmp_path / "progress.md"),
        raising=False,
    )
    saves: list[str] = []

    async def commit(
        *, sequence: int, writes: object, label: str | None = None, **kwargs: object
    ) -> None:
        del sequence, writes, kwargs
        assert label is not None
        saves.append(label)

    monkeypatch.setattr(
        session,
        "ctx",
        SimpleNamespace(log=lambda _message: None, state=SimpleNamespace(commit=commit)),
        raising=False,
    )
    monkeypatch.setattr(
        session, "workspace", SimpleNamespace(revision="trusted-base"), raising=False
    )
    attribution = AsyncMock(
        return_value=(ProfileBottleneck(name="decode", cost=100, share=0.5, evidence=["sample"]),)
    )
    monkeypatch.setattr(profile_session, "run_attribution", attribution)
    rollback = AsyncMock()
    monkeypatch.setattr(session, "_apply_rollback", rollback)

    first = await session.select_hypothesis()

    assert first.selection.hypothesis.hypothesis_id == "h1"
    assert first.selection.hypothesis.parent_commit == "trusted-base"
    assert first.selection.planned_official_reason == "profile-guided component measurement"
    assert session.engine.controller.guidance.active_component == "decode"
    assert saves == ["profile-guided: prepare round 1", "profile_single: start hypothesis h1"]
    assert plan.await_args is not None
    assert plan.await_args.args[0].profile_guidance.active_component == "decode"
    rollback.assert_awaited_once_with(first.selection)

    session.round_number = 2
    continued = await session.select_hypothesis()
    assert continued.request.round_number == 2
    plan.assert_awaited_once()
    attribution.assert_awaited_once()
    assert "continu" in (tmp_path / "progress.md").read_text().lower()


@pytest.mark.asyncio
async def test_paid_attempt_is_marked_before_turn_and_failed_review_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, selected = _session()
    order: list[str] = []
    commit = AsyncMock(side_effect=lambda **_kwargs: order.append("commit"))
    snapshot = AsyncMock(side_effect=lambda _label: order.append("snapshot"))
    reselect = AsyncMock(side_effect=lambda: order.append("reselect"))
    monkeypatch.setattr(
        session,
        "ctx",
        SimpleNamespace(
            log=lambda _message: None,
            state=SimpleNamespace(commit=commit),
            environment=SimpleNamespace(reselect_device=reselect),
        ),
        raising=False,
    )
    monkeypatch.setattr(session, "workspace", SimpleNamespace(snapshot=snapshot), raising=False)
    monkeypatch.setattr(
        session,
        "turns",
        SimpleNamespace(
            progress_path=tmp_path / "progress.md",
            combined=AsyncMock(return_value=_response(Verdict.FAIL)),
        ),
        raising=False,
    )
    session.round_number = 1
    session.records = []

    await session.begin_attempt(selected, 1)
    decision = await session.combined_turn(selected)

    assert decision is AttemptDecision.RETRY
    assert order[:3] == ["commit", "snapshot", "reselect"]
    assert commit.await_count == 2
    assert selected.request.active_hypothesis.feedback == "repair accuracy"
    assert issue_board.next_implementer_attempt(session.turns.progress_path, 1) == 2


@pytest.mark.asyncio
async def test_passing_review_defers_official_gate_until_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, selected = _session()
    controller = session.engine.controller.prepare_round(
        round_number=1,
        attribution=(ProfileBottleneck(name="decode", cost=100, share=0.5, evidence=["sample"]),),
    )
    session.engine = HypothesisEngine(controller)
    assert session.engine.controller.guidance.active_component == "decode"
    selected = ProfileSingleRound(
        RoundSelection(
            selected.selection.state,
            selected.selection.hypothesis,
            selected.selection.plan,
            "profile-guided component measurement",
        ),
        AttemptRequest(
            1,
            selected.request.plan,
            "profile-guided component measurement",
            [],
            selected.request.active_hypothesis,
            "decode",
        ),
        selected.attempt,
    )
    session.round_number = 1
    session.records = []
    monkeypatch.setattr(session, "ctx", SimpleNamespace(log=lambda _message: None), raising=False)
    monkeypatch.setattr(
        session,
        "turns",
        SimpleNamespace(
            progress_path=tmp_path / "progress.md",
            combined=AsyncMock(return_value=_response(Verdict.PASS)),
        ),
        raising=False,
    )

    decision = await session.combined_turn(selected)

    assert decision is AttemptDecision.FINISH
    assert selected.attempt.passed
    assert selected.attempt.official_reason is None
    assert "cadence_not_due" in session.turns.progress_path.read_text()


@pytest.mark.asyncio
async def test_official_gate_failure_keeps_accuracy_evidence_for_same_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`official_gates` policy: ``ctx.gates.run`` mechanics are host-level (see
    ``tests/vibesys/api/test_gates_run.py``)."""
    session, selected = _session()
    session.round_number = 3
    session.records = []
    session.state = session.engine.state
    monkeypatch.setattr(session, "_gate_recorder", None, raising=False)
    commit = AsyncMock()
    monkeypatch.setattr(
        session,
        "ctx",
        SimpleNamespace(
            log=lambda _message: None,
            state=SimpleNamespace(commit=commit),
            gates=SimpleNamespace(
                run=AsyncMock(
                    return_value=GateRunResult(
                        feedback="benchmark failed",
                        benchmark=FrameworkBenchmarkOutcome(),
                        accuracy_passed=True,
                    )
                )
            ),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        session, "workspace", SimpleNamespace(revision="candidate-revision"), raising=False
    )
    monkeypatch.setattr(
        session,
        "turns",
        SimpleNamespace(progress_path=None, worker=SimpleNamespace(backend_name="cli")),
        raising=False,
    )
    selected.attempt.official_reason = "final_round"
    selected.attempt.retry = 2
    monkeypatch.setattr(session, "_record_official_decision", lambda *_args, **_kwargs: None)

    assert not await session.official_gates(selected)
    assert selected.request.active_hypothesis.gate_revalidation_pending
    assert selected.request.active_hypothesis.gate_candidate_commit == "candidate-revision"
    assert selected.request.active_hypothesis.gate_accuracy_passed
    commit.assert_awaited_once()
    assert commit.await_args is not None
    assert commit.await_args.kwargs["label"] == "profile_single: checkpoint hypothesis h1"


@pytest.mark.asyncio
async def test_passed_profile_round_commits_record_through_ctx_state_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`commit_round` hands its record to `ctx.state.commit` and stops there.

    `ROUND_FINISHED`/`EXPERIMENTS_CHANGED` are now derived by the host from
    the committed state (see `vibesys.orchestration.runtime._emit_commit_events`
    and the golden event snapshots), not emitted by this strategy.
    """
    session, selected = _session()
    session.round_number = 1
    session.records = []
    session.state = session.engine.state
    session.framework_benchmark_configured = False
    session.terminal_policy = profile_session._TerminalPolicy()  # noqa: SLF001
    selected.attempt.passed = True
    selected.attempt.retry = 1
    selected.attempt.single_agent_response = _response(Verdict.PASS)
    selected.attempt.judge = JudgeReviewed(Verdict.PASS)
    commit = AsyncMock(return_value="state-revision")
    monkeypatch.setattr(
        session,
        "ctx",
        SimpleNamespace(
            state=SimpleNamespace(commit=commit),
            environment=SimpleNamespace(
                view=SimpleNamespace(paths=SimpleNamespace(accuracy_command=None))
            ),
        ),
        raising=False,
    )
    snapshot = AsyncMock(return_value="candidate-revision")
    monkeypatch.setattr(session, "workspace", SimpleNamespace(snapshot=snapshot), raising=False)
    monkeypatch.setattr(
        session,
        "turns",
        SimpleNamespace(
            progress_path=tmp_path / "progress.md",
            worker=SimpleNamespace(
                backend_name="stub", driver_name=None, provider=None, model="test"
            ),
        ),
        raising=False,
    )

    await session.commit_round(selected)

    assert session.round_number == 2
    assert len(session.state.rounds) == 1
    assert session.state.rounds[0].commit == "candidate-revision"
    assert session.state.rounds[0].judge_verdict == "pass"
    assert session.state.active_hypothesis is None
    commit.assert_awaited_once()
    assert commit.await_args is not None
    assert commit.await_args.kwargs["sequence"] == 1
    assert commit.await_args is not None
    assert commit.await_args.kwargs["writes"]["state.json"] == session.state


def test_retry_cursor_and_official_command_keep_policy_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, selected = _session()
    session.round_number = 1
    session.records = []
    monkeypatch.setattr(session, "ctx", SimpleNamespace(log=lambda _message: None), raising=False)
    monkeypatch.setattr(
        session, "turns", SimpleNamespace(progress_path=tmp_path / "progress.md"), raising=False
    )
    assert list(session.remaining_attempts(selected)) == [1, 2, 3]
    assert session._official_reason(requested=False) is None  # noqa: SLF001
    assert session._official_reason(requested=True) == "orchestrator_request"  # noqa: SLF001
    session.round_number = 3
    assert session._official_reason(requested=False) == "final_round"  # noqa: SLF001
    session.options = session.options.model_copy(update={"max_retries_per_round": 0})
    with pytest.raises(ProfileSingleSessionError, match="exhausting"):
        session.remaining_attempts(selected)
