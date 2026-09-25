"""Multi durable decisions with fake host capabilities."""

# ruff: noqa: SLF001  # These tests exercise the strategy's owned decision seams.

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import AttemptDecision, AttemptState, JudgeReviewed, JudgeSkipped
from vibesys.agent_run.evidence import CarryOver
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.gates import FrameworkBenchmarkOutcome
from vibesys.evaluators.validation_recipe import ValidationRecipeArtifact
from vibesys.loops.multi.decisions import AttemptRequest, HypothesisEngine, RoundSelection
from vibesys.loops.multi.session import (
    MultiRound,
    MultiSession,
    MultiSessionError,
    _TerminalPolicy,
)
from vibesys.orchestration.runtime import GateRunResult, WorkspaceRestoreError
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.schemas import (
    HypothesisOutcome,
    OrchestratorPlan,
)
from vs_loop_state.api import RoundHistory, RoundRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _plan(*, hypothesis_id: str = "h1") -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="Cache decode",
        task="Implement cache",
        pass_criteria="Candidate behaves correctly",  # noqa: S106
        reasoning="The profile identifies decode overhead",
    )


def _selected() -> MultiRound:
    plan = _plan()
    engine = HypothesisEngine.create(AgentRunState()).start(
        plan, started_round=1, parent_commit="a" * 40
    )
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    selection = RoundSelection(engine, engine.state, hypothesis, plan, "final_round")
    request = AttemptRequest(1, plan, "final_round", [], hypothesis, engine, "decode")
    return MultiRound(selection, request, AttemptState(agent_run_state=engine.state, feedback=None))


class _FakeTx:
    """Mirror ``WorkspaceTransaction``'s commit flag for the fake workspace below."""

    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True


@asynccontextmanager
async def _fake_transaction(
    workspace: SimpleNamespace, *, preserve: tuple[str, ...] = (), label: str = "tx"
) -> AsyncIterator[_FakeTx]:
    """Match ``WorkspaceHandle.transaction``'s snapshot/restore-on-exit semantics
    against the same mocked ``snapshot``/``restore`` the fake workspace exposes.
    """
    revision = await workspace.snapshot(label)
    tx = _FakeTx()
    try:
        yield tx
    except BaseException:
        if not tx.committed:
            await workspace.restore(revision, clean=True, preserve_paths=preserve)
        raise
    else:
        if not tx.committed:
            await workspace.restore(revision, clean=True, preserve_paths=preserve)


def _session(tmp_path: Path) -> MultiSession:
    session = cast("Any", MultiSession.__new__(MultiSession))
    state = AgentRunState()
    session.options = SimpleNamespace(
        max_rounds=3,
        max_retries_per_round=2,
        judge_every=1,
        official_eval_every=3,
        metric_space=state.metrics,
    )
    session.state = state
    session.engine = HypothesisEngine.create(state)
    session.records = []
    session.history = RoundHistory(records=[])
    session.carry = CarryOver()
    session.round_number = 1
    session.last_profile_focus = "decode"
    session.framework_benchmark_configured = False
    session.terminal_policy = _TerminalPolicy()
    session.workspace = SimpleNamespace(
        path=tmp_path,
        revision="a" * 40,
        trusted_input_baseline="b" * 40,
        snapshot=AsyncMock(return_value="a" * 40),
        restore=AsyncMock(),
        retain=AsyncMock(),
        pending_changes=AsyncMock(return_value=[]),
    )
    session.workspace.transaction = lambda **kwargs: _fake_transaction(session.workspace, **kwargs)
    progress = tmp_path / "progress.md"
    progress.write_text("# Progress\n")
    session._gate_recorder = issue_board.GateBoardRecorder(progress)
    session.turns = SimpleNamespace(
        progress_path=progress,
        roadmap_path=tmp_path / "roadmap.md",
        objective="Improve throughput",
        worker=SimpleNamespace(
            backend_name="cli",
            driver_name="fake",
            provider="test",
            model="test-model",
        ),
        pre_round_decision=AsyncMock(),
        profile=AsyncMock(),
        plan=AsyncMock(return_value=_plan()),
        implement=AsyncMock(),
        review=AsyncMock(),
        close=AsyncMock(),
    )
    environment = SimpleNamespace(
        run_log_path=tmp_path / "run.log",
        view=SimpleNamespace(
            paths=SimpleNamespace(
                accuracy_command="python check.py",
                benchmark_command="python bench.py",
            ),
            deployment_release_env_var="RELEASE_DEPLOYMENT",
        ),
        reselect_device=AsyncMock(),
        reconcile_model_requests=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=SimpleNamespace(output="ok", exit_code=0)),
    )
    session.ctx = SimpleNamespace(
        log=MagicMock(),
        switch_log=MagicMock(),
        run_configured=MagicMock(),
        warning=MagicMock(),
        events=SimpleNamespace(emit=MagicMock()),
        agents=SimpleNamespace(progress=lambda _progress: nullcontext()),
        state=SimpleNamespace(load=AsyncMock(return_value=None), commit=AsyncMock()),
        environment=environment,
        gates=SimpleNamespace(run=AsyncMock()),
        request=SimpleNamespace(
            project_root=tmp_path,
            input_bundle=SimpleNamespace(benchmark_result=None, benchmark_result_protocol=None),
        ),
    )
    return session


def test_initialize_and_select_checkpoint_after_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = cast("Any", _session(tmp_path))
    asyncio.run(session._initialize())
    assert session.round_number == 1
    assert session.ctx.state.commit.await_count == 1
    assert session.has_next_round

    order: list[str] = []

    async def commit(*, sequence: int, writes: object, **kwargs: object) -> None:
        del sequence, writes
        order.append(f"commit:{kwargs['label']}")

    async def plan(_request: object) -> OrchestratorPlan:
        order.append("plan")
        return _plan()

    session.ctx.state.commit = commit
    session._pre_round_profile = AsyncMock(return_value=None)
    session._apply_rollback = AsyncMock()
    session.turns.plan = plan

    selected = asyncio.run(session.select_hypothesis())
    assert selected.request.plan.hypothesis_id == "h1"
    assert order.index("plan") < order.index("commit:multi: start hypothesis h1")
    assert session.state.active_hypothesis is not None

    monkeypatch.setattr(
        "vibesys.loops.multi.session.issue_board.append_hypothesis_continuation",
        MagicMock(),
    )
    session.turns.plan = AsyncMock(side_effect=AssertionError("designer must be skipped"))
    continued = asyncio.run(session.select_hypothesis())
    assert continued.request.plan.hypothesis_id == "h1"
    session.turns.plan.assert_not_awaited()


def test_attempt_preflight_and_unparseable_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = cast("Any", _session(tmp_path))
    selected = _selected()
    monkeypatch.setattr(
        "vibesys.loops.multi.session.issue_board.next_implementer_attempt",
        lambda _path, _round: 2,
    )
    assert list(session.remaining_attempts(selected)) == [2]
    asyncio.run(session.begin_attempt(selected, 2))
    assert selected.attempt.retry == 2
    assert isinstance(selected.attempt.judge, JudgeSkipped)
    session.ctx.state.commit.assert_awaited_once()
    session.ctx.environment.reselect_device.assert_awaited_once()

    response = ImplementerResponse(summary="unparseable", expected_behavior="unknown")
    session.turns.implement = AsyncMock(return_value=(response, True))
    assert not asyncio.run(session.implement(selected))
    assert selected.attempt.implementation == response

    monkeypatch.setattr(
        "vibesys.loops.multi.session.issue_board.next_implementer_attempt",
        lambda _path, _round: 3,
    )
    with pytest.raises(MultiSessionError, match="exhausting"):
        session.remaining_attempts(selected)


def test_review_retries_rejection_and_validation_before_official_gate(tmp_path: Path) -> None:
    session = cast("Any", _session(tmp_path))
    selected = _selected()
    session._validate_local = AsyncMock(return_value=None)
    session._approve_candidate = AsyncMock()
    session._approve_perf = AsyncMock()
    session.options.max_rounds = 1

    with pytest.raises(MultiSessionError, match="review requires"):
        asyncio.run(session.review(selected))

    selected.attempt.implementation = ImplementerResponse(
        summary="changed",
        expected_behavior="faster",
        hypothesis_outcome=HypothesisOutcome.SUPPORTED,
    )
    session.turns.review = AsyncMock(
        return_value=JudgeResponse(analysis="bad", feedback="repair", verdict=Verdict.FAIL)
    )
    assert asyncio.run(session.review(selected)) is AttemptDecision.RETRY
    assert selected.attempt.feedback == "repair"
    session.ctx.state.commit.assert_awaited()

    session.turns.review.return_value = JudgeResponse(
        analysis="good", feedback="", verdict=Verdict.PASS
    )
    session._validate_local.return_value = "local recipe failed"
    assert asyncio.run(session.review(selected)) is AttemptDecision.RETRY
    assert selected.attempt.feedback == "local recipe failed"

    session._validate_local.return_value = None
    assert asyncio.run(session.review(selected)) is AttemptDecision.OFFICIAL
    assert selected.attempt.official_reason is not None
    session._approve_perf.assert_awaited_once()


def test_sparse_review_defers_gates_and_records_provisional_decision(tmp_path: Path) -> None:
    session = cast("Any", _session(tmp_path))
    selected = _selected()
    selected.attempt.implementation = ImplementerResponse(
        summary="continue",
        expected_behavior="faster",
        hypothesis_outcome=HypothesisOutcome.CONTINUE,
        next_step="Finish cache",
    )
    session.options.judge_every = 3
    assert asyncio.run(session.review(selected)) is AttemptDecision.FINISH
    session.turns.review.assert_not_awaited()

    selected.attempt.implementation = ImplementerResponse(
        summary="candidate",
        expected_behavior="faster",
        hypothesis_outcome=HypothesisOutcome.SUPPORTED,
    )
    session.turns.review = AsyncMock(
        return_value=JudgeResponse(analysis="good", feedback="", verdict=Verdict.PASS)
    )
    session._validate_local = AsyncMock(return_value=None)
    session._approve_candidate = AsyncMock()
    session._record_official_decision = MagicMock()
    assert asyncio.run(session.review(selected)) is AttemptDecision.FINISH
    assert selected.attempt.passed
    session._record_official_decision.assert_called_once_with(
        selected, run=False, reason="cadence_not_due"
    )


def test_official_gate_feedback_is_checkpointed_and_success_passes(tmp_path: Path) -> None:
    """`official_gates` handles `ctx.gates.run`'s result: the policy the strategy owns.

    `ctx.gates.run`'s own mechanics (stub skip, resource reconciliation,
    accuracy-then-benchmark ordering) are host mechanics, covered at the host
    level in `tests/vibesys/api/test_gates_run.py`.
    """
    session = cast("Any", _session(tmp_path))
    selected = _selected()
    selected.attempt.official_reason = "final_round"
    selected.attempt.retry = 1
    session._record_official_decision = MagicMock()
    benchmark = FrameworkBenchmarkOutcome(metric_name="throughput", metric_value=42.0)
    session.ctx.gates.run.return_value = GateRunResult(
        feedback="benchmark failed", benchmark=benchmark, accuracy_passed=True
    )

    assert not asyncio.run(session.official_gates(selected))
    assert selected.request.active_hypothesis.gate_revalidation_pending
    assert selected.request.active_hypothesis.gate_accuracy_passed
    assert selected.attempt.framework_perf_metric == 42.0
    session.ctx.state.commit.assert_awaited_once()

    session.ctx.gates.run.return_value = GateRunResult(
        feedback=None, benchmark=benchmark, accuracy_passed=True
    )
    assert asyncio.run(session.official_gates(selected))
    assert selected.attempt.passed


def test_local_validation_rejects_mutating_command_and_restores_snapshot(tmp_path: Path) -> None:
    session = cast("Any", _session(tmp_path))
    selected = _selected()
    selected.attempt.retry = 1
    (tmp_path / "candidate.py").write_text("candidate = 1\n")
    recipe = {
        "name": "focused",
        "command": "python check.py",
        "input_paths": ["candidate.py"],
        "purpose": "Check candidate",
    }
    (tmp_path / "recipes.json").write_text(
        ValidationRecipeArtifact.model_validate({"recipes": [recipe]}).model_dump_json()
    )
    session.workspace.pending_changes.return_value = ["candidate.py"]
    feedback = asyncio.run(session._validate_local(selected, "recipes.json"))
    assert feedback is not None
    assert "mutated the workspace" in feedback
    session.workspace.restore.assert_awaited_once()
    artifact = tmp_path / "progress-artifacts" / "validation" / "round-0001-attempt-01.json"
    assert json.loads(artifact.read_text())["results"][0]["passed"] is False


def test_finalization_restores_baseline_when_no_candidate(tmp_path: Path) -> None:
    session = cast("Any", _session(tmp_path))
    assert asyncio.run(session.finish())
    session.workspace.restore.assert_awaited_once()
    assert session.workspace.restore.call_args.args[0] == "b" * 40
    assert asyncio.run(session.close()) is None
    session.turns.close.assert_awaited_once()


def test_completed_reviewed_round_checkpoints_record_before_advancing(tmp_path: Path) -> None:
    session = cast("Any", _session(tmp_path))
    selected = _selected()
    session.engine = selected.selection.engine
    session.state = selected.selection.state
    selected.attempt.retry = 2
    selected.attempt.passed = True
    selected.attempt.official_reason = "final_round"
    selected.attempt.judge = JudgeReviewed(Verdict.PASS)
    selected.attempt.implementation = ImplementerResponse(
        summary="cache added",
        expected_behavior="faster",
        hypothesis_outcome=HypothesisOutcome.NOMINATED,
        perf_metric=42.0,
        perf_unit="throughput",
    )

    asyncio.run(session.commit_round(selected))

    assert session.round_number == 2
    assert len(session.records) == 1
    record = session.records[0]
    assert record.hypothesis_id == "h1"
    assert record.official_evaluation
    assert record.passed
    commit = session.ctx.state.commit.await_args
    assert commit.kwargs["sequence"] == 1
    assert commit.kwargs["writes"]["state.json"].rounds == session.records


def _rollback_selection(tmp_path: Path, *, parent_round: int, started_round: int) -> RoundSelection:
    del tmp_path
    plan = _plan().model_copy(update={"revert_to_round": parent_round})
    engine = HypothesisEngine.create(AgentRunState()).start(
        plan,
        started_round=started_round,
        parent_round=parent_round,
        parent_commit="c" * 40,
    )
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    return RoundSelection(engine, engine.state, hypothesis, plan, None)


def test_apply_rollback_warns_and_retries_on_checkout_failure(tmp_path: Path) -> None:
    """R1 regression: a rollback checkout failure must warn, not abort the run.

    Before this fix, ``workspace.restore`` (``_Workspaces.adopt`` in
    ``orchestration/runtime.py``) raised a bare ``RuntimeError`` on a failed
    ``git checkout``, which propagated out of ``_apply_rollback`` and crashed
    the run. Base behavior was to warn and retry the rollback on a later
    round, leaving ``hypothesis.revert_applied`` false until it succeeds.
    """
    session = cast("Any", _session(tmp_path))
    session.records = [
        RoundRecord(round_number=1, commit="c" * 40, perf_metric=None, perf_unit=None, passed=True)
    ]
    session.history = RoundHistory(records=[])
    session.round_number = 2
    selection = _rollback_selection(tmp_path, parent_round=1, started_round=2)
    session.workspace.restore = AsyncMock(side_effect=WorkspaceRestoreError("c" * 40))

    asyncio.run(session._apply_rollback(selection))

    assert selection.hypothesis.revert_applied is False
    session.ctx.warning.assert_called_once()
    session.ctx.state.commit.assert_not_awaited()


@given(
    parent_round=st.integers(min_value=1, max_value=20),
    fails=st.lists(st.booleans(), min_size=1, max_size=10),
)
@settings(max_examples=25, deadline=None)
def test_apply_rollback_eventually_applies_once_checkout_succeeds(
    parent_round: int, fails: list[bool], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Property: whatever pattern of checkout failures precedes it, a later successful
    checkout applies the rollback exactly once, and the run never raises meanwhile.
    """
    tmp_path = tmp_path_factory.mktemp("rollback")
    session = cast("Any", _session(tmp_path))
    session.records = [
        RoundRecord(
            round_number=parent_round,
            commit="c" * 40,
            perf_metric=None,
            perf_unit=None,
            passed=True,
        )
    ]
    session.history = RoundHistory(records=[])
    selection = _rollback_selection(
        tmp_path, parent_round=parent_round, started_round=parent_round + 1
    )

    for round_number, should_fail in enumerate(fails, start=parent_round + 1):
        session.round_number = round_number
        if should_fail:
            session.workspace.restore = AsyncMock(side_effect=WorkspaceRestoreError("c" * 40))
        else:
            session.workspace.restore = AsyncMock(return_value=None)
        asyncio.run(session._apply_rollback(selection))
        if should_fail:
            assert selection.hypothesis.revert_applied is False
        else:
            assert selection.hypothesis.revert_applied is True
        if selection.hypothesis.revert_applied:
            break

    # Once applied, a further call is a no-op (idempotent; state is consistent).
    session.workspace.restore = AsyncMock(side_effect=WorkspaceRestoreError("c" * 40))
    asyncio.run(session._apply_rollback(selection))
    if any(not f for f in fails):
        assert selection.hypothesis.revert_applied is True
