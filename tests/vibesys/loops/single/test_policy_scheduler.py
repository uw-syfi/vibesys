"""Single strategy terminal decisions over completed round evidence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import AttemptDecision, AttemptState, JudgeReviewed
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.options import AgentOrchestrationOptions
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.loops.single import session as single_session
from vibesys.loops.single.hypothesis import HypothesisEngine
from vibesys.loops.single.session import (
    AttemptRequest,
    RoundSelection,
    SingleRound,
    SingleSession,
    SingleSessionError,
)
from vibesys.orchestration.runtime import GateRunResult, WorkspaceRestoreError
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


def _session() -> tuple[SingleSession, SingleRound]:
    plan = _plan()
    engine = HypothesisEngine.create(AgentRunState()).start(plan, started_round=1)
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    session = SingleSession.__new__(SingleSession)
    session.engine = engine
    session.options = AgentOrchestrationOptions(
        interface="inprocess",
        max_rounds=3,
        max_retries_per_round=3,
        judge_every=1,
        official_eval_every=2,
        memory_layout="files",
    )
    selection = RoundSelection(engine.state, hypothesis, plan, None)
    request = AttemptRequest(1, plan, None, [], hypothesis, "latency")
    attempt = AttemptState(agent_run_state=engine.state, feedback=None)
    return session, SingleRound(selection, request, attempt)


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

    engine, carry, exhaustion = session.complete_policy_round(selected, record)

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

    engine, carry, exhaustion = session.complete_policy_round(selected, record)

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
async def test_new_plan_checkpoints_hypothesis_then_continues_without_new_designer_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = AgentRunState()
    session = SingleSession.__new__(SingleSession)
    session.options = _session()[0].options
    session.state = state
    session.engine = HypothesisEngine.create(state)
    session.records = []
    session.round_number = 1
    session.carry = CarryOver()
    session.last_response = None
    session.last_profile_focus = "decode"
    plan = AsyncMock(return_value=_plan())
    monkeypatch.setattr(
        session,
        "turns",
        SimpleNamespace(plan=plan, progress_path=tmp_path / "progress.md"),
        raising=False,
    )
    commit = AsyncMock()
    monkeypatch.setattr(
        session,
        "ctx",
        SimpleNamespace(log=lambda _message: None, state=SimpleNamespace(commit=commit)),
        raising=False,
    )
    monkeypatch.setattr(
        session, "workspace", SimpleNamespace(revision="trusted-base"), raising=False
    )
    rollback = AsyncMock()
    monkeypatch.setattr(session, "_apply_rollback", rollback)

    first = await session.select_hypothesis()

    assert first.selection.hypothesis.hypothesis_id == "h1"
    assert first.selection.hypothesis.parent_commit == "trusted-base"
    commit.assert_awaited_once()
    assert commit.await_args is not None
    assert commit.await_args.kwargs["label"] == "single: start hypothesis h1"
    rollback.assert_awaited_once_with(first.selection)
    assert plan.await_args is not None
    assert plan.await_args.args[0].profile_guidance.plan_prompt_context() == {}

    session.round_number = 2
    continued = await session.select_hypothesis()
    assert continued.selection.hypothesis.hypothesis_id == "h1"
    assert continued.request.round_number == 2
    plan.assert_awaited_once()
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
    assert commit.await_args.kwargs["label"] == "single: checkpoint hypothesis h1"


@pytest.mark.asyncio
async def test_passed_round_commits_record_through_ctx_state_commit(
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
    session.terminal_policy = single_session._TerminalPolicy()  # noqa: SLF001
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
    with pytest.raises(SingleSessionError, match="exhausting"):
        session.remaining_attempts(selected)


def _rollback_session(*, parent_round: int, started_round: int) -> tuple[Any, RoundSelection]:
    plan = _plan().model_copy(update={"revert_to_round": parent_round})
    engine = HypothesisEngine.create(AgentRunState()).start(
        plan, started_round=started_round, parent_round=parent_round, parent_commit="c" * 40
    )
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    session = cast("Any", SingleSession.__new__(SingleSession))
    session.engine = engine
    session.state = engine.state
    session.records = [
        RoundRecord(
            round_number=parent_round,
            commit="c" * 40,
            perf_metric=None,
            perf_unit=None,
            passed=True,
        )
    ]
    session.history = SimpleNamespace(
        resolve_rollback_commit=lambda target, _outcomes: (target.commit, None)
    )
    return session, RoundSelection(engine.state, hypothesis, plan, None)


def test_apply_rollback_warns_and_retries_on_checkout_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 regression: a rollback checkout failure must warn, not abort the run.

    Base behavior (before the PR 942 regression) warned and retried the
    rollback on a later round; a bare ``RuntimeError`` from a failed Git
    checkout instead propagated out of ``_apply_rollback`` and aborted the
    whole run.
    """
    session, selection = _rollback_session(parent_round=1, started_round=2)
    session.round_number = 2
    warnings: list[str] = []
    monkeypatch.setattr(session, "ctx", SimpleNamespace(warning=warnings.append), raising=False)
    monkeypatch.setattr(session, "_memory_paths", lambda: (), raising=False)
    monkeypatch.setattr(
        session,
        "workspace",
        SimpleNamespace(
            restore=AsyncMock(side_effect=WorkspaceRestoreError("c" * 40)),
            path=tmp_path,
        ),
        raising=False,
    )

    asyncio.run(session._apply_rollback(selection))  # noqa: SLF001

    assert selection.hypothesis.revert_applied is False
    assert warnings


@given(fails=st.lists(st.booleans(), min_size=1, max_size=8))
@settings(max_examples=25, deadline=None)
def test_apply_rollback_eventually_applies_once_checkout_succeeds(
    fails: list[bool], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Property: rollback is applied exactly once checkout succeeds, run never aborts."""
    tmp_path = tmp_path_factory.mktemp("single-rollback")
    session, selection = _rollback_session(parent_round=1, started_round=2)
    session.ctx = SimpleNamespace(warning=lambda _message: None, log=lambda _message: None)
    session._memory_paths = lambda: ()  # noqa: SLF001

    async def commit(**_kwargs: object) -> None:
        return None

    session.ctx.state = SimpleNamespace(commit=commit)

    for round_number, should_fail in enumerate(fails, start=2):
        session.round_number = round_number
        if should_fail:
            session.workspace = SimpleNamespace(
                restore=AsyncMock(side_effect=WorkspaceRestoreError("c" * 40)), path=tmp_path
            )
        else:
            session.workspace = SimpleNamespace(restore=AsyncMock(return_value=None), path=tmp_path)
        asyncio.run(session._apply_rollback(selection))  # noqa: SLF001
        assert selection.hypothesis.revert_applied == (not should_fail)
        if selection.hypothesis.revert_applied:
            break
