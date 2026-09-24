"""Guard-clause and failure-path tests for the agent loop's private helpers.

Each helper is exercised through fakes for the collaborators it touches, so the
tests pin the raised error messages, the returned control flow, and the state
they mutate without running a real round.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from tests.support import make_orchestrator_plan

from vibesys.api.contracts import ResumeRef
from vibesys.constants import DomainName
from vibesys.loops.agent import issue_board
from vibesys.loops.agent import loop as agent_loop
from vibesys.loops.agent.attempt import JudgeReviewed
from vibesys.loops.agent.hypotheses import start_hypothesis
from vibesys.loops.agent.hypothesis_controller import HypothesisEngine
from vibesys.loops.agent.loop import (
    _ROLE_CHANGE_DISPLAY_LIMIT,
    _apply_agent_rollback,
    _begin_completed_agent_round,
    _candidate_evidence,
    _complete_hypothesis_round,
    _create_agent_run_context,
    _finalize_agent_run,
    _finish_multi_agent_pass,
    _handle_agent_round_limit,
    _invoke_read_only_role,
    _load_validation_recipes,
    _RoundAttemptOutcome,
    _run_agent_attempts,
    _run_judge,
    _run_single_agent_retry,
    _validate_agent_request,
    _validate_orchestrator_plan_state,
    _validation_input_digest,
)
from vibesys.loops.agent.model import AgentRunState, Hypothesis
from vibesys.loops.metrics import MetricSpace
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    HypothesisStrategyUpdate,
    OrchestratorPlan,
    ValidationRecipe,
    Verdict,
)
from vs_loop_state.api import RoundRecord

if TYPE_CHECKING:
    from vibesys.loops.agent.loop import LoopContext
    from vibesys.loops.request import LoopRunRequest


def _plan(identifier: str, **fields: object) -> OrchestratorPlan:
    return make_orchestrator_plan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        task=f"implement {identifier}",
        criteria="tests pass",
        reasoning="test the claim",
        **fields,
    )


def _ctx(**members: object) -> "LoopContext":
    ctx = MagicMock()
    for name, value in members.items():
        setattr(ctx, name, value)
    return cast("LoopContext", ctx)


def _request(**fields: object) -> "LoopRunRequest":
    return cast("LoopRunRequest", SimpleNamespace(**fields))


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def _valid_request_fields() -> dict[str, Any]:
    return {
        "inner_loop": "multi-agent",
        "max_retries_per_round": 3,
        "judge_every": 3,
        "official_eval_every": 1,
        "memory_layout": "files",
        "interface": "inprocess",
        "loop": SimpleNamespace(value="agent"),
        "input_bundle": SimpleNamespace(
            domain=next(iter(DomainName)),
            manifest=SimpleNamespace(profile_guided=None),
        ),
    }


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"inner_loop": "swarm"}, "Unknown inner_loop 'swarm'; choose from multi-agent"),
        ({"max_retries_per_round": 0}, "max_retries_per_round must be >= 1, got 0"),
        ({"judge_every": 0}, "judge_every must be >= 1, got 0"),
        ({"official_eval_every": -2}, "official_eval_every must be >= 1, got -2"),
        ({"memory_layout": "blob"}, "Unknown memory_layout 'blob'; choose from files"),
        ({"interface": "carrier-pigeon"}, "Unknown interface 'carrier-pigeon'; choose from"),
        (
            {"loop": SimpleNamespace(value="profile-guided")},
            "profile-guided runs require profile guidance settings",
        ),
    ],
)
def test_validate_agent_request_rejects_bad_options(override: dict[str, Any], message: str) -> None:
    request = _request(**{**_valid_request_fields(), **override})
    with pytest.raises(ValueError, match=message):
        _validate_agent_request(request)


def test_validate_agent_request_resolves_the_requested_domain() -> None:
    fields = _valid_request_fields()
    domain = _validate_agent_request(_request(**fields))
    assert domain.prompt_dir.is_dir()


def test_fresh_run_requires_an_experiment_name() -> None:
    request = _request(
        input_bundle=SimpleNamespace(),
        resume=None,
        exp_name=None,
    )
    with pytest.raises(ValueError, match="exp_name must be set for a fresh"):
        _create_agent_run_context(request, None)


# --------------------------------------------------------------------------
# Round limit
# --------------------------------------------------------------------------


def test_round_limit_finalizes_a_completed_resumed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    finalized: list[dict[str, Any]] = []
    monkeypatch.setattr(agent_loop, "_finalize_agent_run", lambda _ctx, **kw: finalized.append(kw))
    ctx = _ctx()
    records = [SimpleNamespace(round_number=1)]
    request = _request(max_rounds=1, resume=ResumeRef(run_id="r1"), metrics=MetricSpace())

    result = _handle_agent_round_limit(
        ctx,
        request,
        cast("Any", SimpleNamespace(records=records)),
        Path("progress.md"),
        2,
    )

    assert result is True
    assert finalized == [
        {"records": records, "space": request.metrics, "progress_path": Path("progress.md")}
    ]
    cast("MagicMock", ctx).close.assert_called_once_with()


def test_round_limit_rejects_an_exhausted_fresh_budget() -> None:
    ctx = _ctx()
    request = _request(max_rounds=2, resume=None, metrics=MetricSpace())
    history = cast("Any", SimpleNamespace(records=[]))

    with pytest.raises(ValueError, match=r"completed 2 rounds; max_rounds=2 is a total limit"):
        _handle_agent_round_limit(ctx, request, history, Path("progress.md"), 3)

    cast("MagicMock", ctx).close.assert_called_once_with()
    assert _handle_agent_round_limit(ctx, request, history, Path("progress.md"), 2) is None


# --------------------------------------------------------------------------
# Plan validation
# --------------------------------------------------------------------------


def test_orchestrator_plan_state_rejects_duplicate_self_and_reused_updates() -> None:
    def update(identifier: str) -> HypothesisStrategyUpdate:
        return HypothesisStrategyUpdate(
            hypothesis_id=identifier, disposition="parked", reason="not now"
        )

    state = AgentRunState()
    with pytest.raises(ValueError, match="must name each hypothesis once"):
        _validate_orchestrator_plan_state(
            _plan("new", hypothesis_updates=[update("old"), update("old")]), state
        )
    with pytest.raises(ValueError, match="must refer to prior hypotheses"):
        _validate_orchestrator_plan_state(_plan("new", hypothesis_updates=[update("new")]), state)
    started = start_hypothesis(AgentRunState(), _plan("used"), started_round=1)
    with pytest.raises(ValueError, match="hypothesis ID 'used' was already used"):
        _validate_orchestrator_plan_state(_plan("used"), started)


# --------------------------------------------------------------------------
# Validation recipes
# --------------------------------------------------------------------------


def _recipe(*paths: str) -> ValidationRecipe:
    return ValidationRecipe(
        name="focused",
        command="pytest -q",
        input_paths=list(paths),
        purpose="check it",
    )


def test_validation_digest_hashes_contents_and_recipe_fields(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a = 1\n")
    recipe = _recipe("src")

    first = _validation_input_digest(tmp_path, recipe)
    assert first == _validation_input_digest(tmp_path, recipe)
    (tmp_path / "src" / "a.py").write_text("a = 2\n")
    assert _validation_input_digest(tmp_path, recipe) != first
    changed = recipe.model_copy(update={"command": "pytest -x"})
    assert _validation_input_digest(tmp_path, changed) != _validation_input_digest(tmp_path, recipe)


def test_validation_digest_rejects_symlinks_escapes_and_missing_inputs(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("x")
    (workspace / "link.txt").symlink_to(tmp_path / "outside.txt")
    (tmp_path / "outside_dir").mkdir()
    (tmp_path / "outside_dir" / "file.txt").write_text("x")
    (workspace / "linkdir").symlink_to(tmp_path / "outside_dir")
    (workspace / "dir").mkdir()
    (workspace / "dir" / "inner-link.txt").symlink_to(tmp_path / "outside.txt")

    with pytest.raises(ValueError, match=r"must not be a symlink: link\.txt"):
        _validation_input_digest(workspace, _recipe("link.txt"))
    with pytest.raises(ValueError, match=r"escapes workspace: linkdir/file\.txt"):
        _validation_input_digest(workspace, _recipe("linkdir/file.txt"))
    with pytest.raises(ValueError, match=r"does not exist: nope\.txt"):
        _validation_input_digest(workspace, _recipe("nope.txt"))
    with pytest.raises(ValueError, match="must not be a symlink: dir"):
        _validation_input_digest(workspace, _recipe("dir"))


def test_validation_digest_enforces_the_reuse_hash_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("data")
    monkeypatch.setattr(agent_loop, "_MAX_VALIDATION_INPUT_FILES", 1)
    with pytest.raises(ValueError, match="exceed the 4096-file/256-MiB reuse-hash limit"):
        _validation_input_digest(tmp_path, _recipe("a.txt", "b.txt"))
    monkeypatch.setattr(agent_loop, "_MAX_VALIDATION_INPUT_FILES", 10)
    monkeypatch.setattr(agent_loop, "_MAX_VALIDATION_INPUT_BYTES", 5)
    with pytest.raises(ValueError, match="exceed the 4096-file/256-MiB reuse-hash limit"):
        _validation_input_digest(tmp_path, _recipe("a.txt", "b.txt"))


def test_load_validation_recipes_returns_valid_recipes(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "recipes": [
            {
                "name": "focused-tests",
                "command": "pytest -q",
                "input_paths": ["tests"],
                "purpose": "run the tests",
            }
        ],
    }
    (tmp_path / "recipes.json").write_text(json.dumps(payload))

    recipes = _load_validation_recipes(tmp_path, "recipes.json")

    assert [recipe.name for recipe in recipes] == ["focused-tests"]
    assert recipes[0].input_paths == ["tests"]


@pytest.mark.parametrize(
    ("artifact", "content", "message"),
    [
        ("../recipes.json", None, "artifact escapes the workspace"),
        ("missing.json", None, "artifact does not exist: missing.json"),
        ("bad.json", "{not json", "artifact is not valid JSON"),
        ("shape.json", '{"version": 2, "recipes": []}', "does not match version 1"),
    ],
)
def test_load_validation_recipes_rejects_bad_artifacts(
    tmp_path: Path, artifact: str, content: str | None, message: str
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    if content is not None:
        (workspace / artifact).write_text(content)
    with pytest.raises(ValueError, match=message):
        _load_validation_recipes(workspace, artifact)


# --------------------------------------------------------------------------
# Finalization and role isolation
# --------------------------------------------------------------------------


def test_finalize_fails_when_the_trusted_baseline_cannot_be_restored(tmp_path: Path) -> None:
    ctx = MagicMock()
    ctx.workspace = tmp_path
    ctx.git.trusted_input_baseline = "b" * 40
    ctx.git.checkout_tree.return_value = False

    with pytest.raises(RuntimeError, match="could not restore trusted input baseline"):
        _finalize_agent_run(
            ctx, records=[], space=MetricSpace(), progress_path=tmp_path / "progress.md"
        )
    ctx.snapshot_workspace.assert_not_called()


@pytest.mark.parametrize(
    ("commit", "checkout", "message"),
    [
        (None, True, "selected final candidate is missing its commit"),
        ("c" * 40, False, "could not materialize selected round 3 at"),
    ],
)
def test_finalize_fails_on_a_broken_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit: str | None,
    *,
    checkout: bool,
    message: str,
) -> None:
    winner = SimpleNamespace(commit=commit, round_number=3)
    monkeypatch.setattr(agent_loop, "_select_final_candidate", lambda *_a, **_k: winner)
    ctx = MagicMock()
    ctx.workspace = tmp_path
    ctx.git.checkout_tree.return_value = checkout

    with pytest.raises(RuntimeError, match=message):
        _finalize_agent_run(
            ctx, records=[], space=MetricSpace(), progress_path=tmp_path / "progress.md"
        )
    ctx.snapshot_workspace.assert_not_called()


def _read_only_kwargs() -> dict[str, Any]:
    return {
        "role": "judge",
        "checkpoint_label": "label",
        "kind": "judge",
        "system_prompt": "s",
        "user_prompt": "u",
        "response_cls": OrchestratorPlan,
        "fallback_factory": lambda: None,
    }


def test_read_only_role_requires_a_workspace_checkpoint() -> None:
    ctx = MagicMock()
    ctx.git.current_sha.return_value = None
    with pytest.raises(RuntimeError, match="Cannot isolate judge: workspace checkpoint"):
        _invoke_read_only_role(cast("LoopContext", ctx), **_read_only_kwargs())
    ctx.invoke.assert_not_called()


def test_read_only_role_fails_when_the_checkpoint_cannot_be_restored() -> None:
    ctx = MagicMock()
    ctx.git.current_sha.return_value = "a" * 40
    ctx.git.pending_changes.return_value = ["main.py"]
    ctx.git.checkout_tree.return_value = False
    with pytest.raises(RuntimeError, match="failed to restore workspace checkpoint aaaaaaaaaaaa"):
        _invoke_read_only_role(cast("LoopContext", ctx), **_read_only_kwargs())


def test_read_only_role_fails_when_the_workspace_stays_modified() -> None:
    ctx = MagicMock()
    ctx.git.current_sha.return_value = "a" * 40
    ctx.git.pending_changes.return_value = ["main.py", "other.py"]
    ctx.git.checkout_tree.return_value = True
    with pytest.raises(RuntimeError, match=r"still modified after restore: main\.py, other\.py"):
        _invoke_read_only_role(cast("LoopContext", ctx), **_read_only_kwargs())


def test_read_only_role_summarizes_many_reverted_changes() -> None:
    limit = _ROLE_CHANGE_DISPLAY_LIMIT
    changed = [f"f{index}.py" for index in range(limit + 3)]
    ctx = MagicMock()
    ctx.git.current_sha.return_value = "a" * 40
    ctx.git.pending_changes.side_effect = [changed, []]
    ctx.git.checkout_tree.return_value = True

    _invoke_read_only_role(cast("LoopContext", ctx), **_read_only_kwargs())

    logged = ctx.lprint.call_args.args[0]
    assert f"reverted {limit + 3} workspace change(s) attempted by judge" in logged
    assert logged.endswith(", ... (+3 more)")
    assert "f0.py" in logged
    assert f"f{limit}.py" not in logged


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------


def _engine_with_active(identifier: str = "H-1", **fields: object) -> HypothesisEngine:
    hypothesis = Hypothesis(
        hypothesis_id=identifier, plan=_plan(identifier), started_round=1, **fields
    )
    state = AgentRunState(active_hypothesis_id=identifier, hypotheses=[hypothesis])
    return HypothesisEngine.create(state, config=None)


def test_rollback_requires_an_active_hypothesis() -> None:
    engine = HypothesisEngine.create(AgentRunState(), config=None)
    with pytest.raises(RuntimeError, match="did not retain the current hypothesis"):
        _apply_agent_rollback(_ctx(), _request(), _plan("x"), cast("Any", None), engine)


def test_rollback_is_a_no_op_without_a_target_or_after_it_was_applied() -> None:
    engine = _engine_with_active()
    history = cast("Any", SimpleNamespace(records=[]))
    assert _apply_agent_rollback(_ctx(), _request(), _plan("x"), history, engine) is engine
    applied = _engine_with_active(revert_applied=True)
    revert = _plan("x", revert_to_round=1)
    assert _apply_agent_rollback(_ctx(), _request(), revert, history, applied) is applied


def test_rollback_warns_when_the_target_round_has_no_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        agent_loop,
        "output_sink",
        lambda: SimpleNamespace(framework_warning=lambda text, **_kw: warnings.append(text)),
    )
    engine = _engine_with_active()
    history = cast("Any", SimpleNamespace(records=[SimpleNamespace(round_number=1, commit=None)]))

    result = _apply_agent_rollback(
        _ctx(), _request(), _plan("x", revert_to_round=1), history, engine
    )

    assert result is engine
    assert warnings == ["cannot revert: no commit recorded for round 1"]


def _rollback_history(commit: str | None) -> SimpleNamespace:
    target = SimpleNamespace(round_number=1, commit="a" * 40)
    return SimpleNamespace(
        records=[target],
        resolve_rollback_commit=lambda *_a: (commit, None),
    )


def test_rollback_fails_when_no_rollback_commit_resolves() -> None:
    with pytest.raises(RuntimeError, match="rollback target round 1 has no commit"):
        _apply_agent_rollback(
            _ctx(),
            _request(memory_layout="files"),
            _plan("x", revert_to_round=1),
            cast("Any", _rollback_history(None)),
            _engine_with_active(),
        )


def test_rollback_warns_and_keeps_the_engine_when_checkout_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        agent_loop,
        "output_sink",
        lambda: SimpleNamespace(framework_warning=lambda text, **_kw: warnings.append(text)),
    )
    ctx = MagicMock()
    ctx.workspace = tmp_path
    ctx.git.checkout_tree.return_value = False
    engine = _engine_with_active()

    result = _apply_agent_rollback(
        cast("LoopContext", ctx),
        _request(memory_layout="files"),
        _plan("x", revert_to_round=1),
        cast("Any", _rollback_history("d" * 40)),
        engine,
    )

    assert result is engine
    assert warnings == ["rollback was not applied; will retry round 1 on the next continuation"]


# --------------------------------------------------------------------------
# Engine invariants and completion
# --------------------------------------------------------------------------


def test_completing_a_round_needs_a_retained_active_hypothesis() -> None:
    engine = HypothesisEngine.create(AgentRunState(), config=None)
    record = RoundRecord(
        round_number=1, commit="1" * 40, perf_metric=None, perf_unit=None, passed=False
    )
    attempt = _RoundAttemptOutcome(passed=False, feedback=None, implementation=None)
    with pytest.raises(RuntimeError, match="did not retain the current hypothesis"):
        _begin_completed_agent_round(_ctx(), _request(), engine, record, attempt)


def test_unreviewed_failed_round_keeps_the_hypothesis_active_without_feedback() -> None:
    engine = _engine_with_active(feedback="old feedback", next_step="old step")
    hypothesis = engine.state.active_hypothesis
    assert hypothesis is not None
    record = RoundRecord(
        round_number=2,
        commit="2" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=False,
        reviewed=False,
        hypothesis_id="H-1",
    )
    request = _request(inner_loop="single-agent", loop=SimpleNamespace(value="agent"))

    updated = _complete_hypothesis_round(
        engine,
        hypothesis,
        record,
        request,
        _RoundAttemptOutcome(passed=False, feedback="ignored", implementation=None),
    )

    active = updated.state.active_hypothesis
    assert active is not None
    assert active.feedback is None
    assert active.next_step is None
    assert active.continuation_rounds == hypothesis.continuation_rounds


# --------------------------------------------------------------------------
# Candidate evidence
# --------------------------------------------------------------------------


def _candidate_row(**fields: object) -> SimpleNamespace:
    row = {
        "candidate_disposition": CandidateDisposition.PARETO_FRONTIER,
        "candidate_metrics": {"throughput": 9.0},
        "candidate_evaluation_artifact": "eval.json",
        "candidate_operating_point": "batch=8",
        "candidate_retention_reason": "best latency",
    }
    return SimpleNamespace(**{**row, **fields})


@pytest.mark.parametrize("source", ["implementation", "single_agent_response"])
def test_candidate_evidence_reads_the_final_attempt(source: str) -> None:
    attempt = _RoundAttemptOutcome(
        passed=True,
        feedback=None,
        implementation=_candidate_row() if source == "implementation" else None,
        single_agent_response=_candidate_row() if source == "single_agent_response" else None,
    )
    evidence = _candidate_evidence(attempt, _engine_with_active().state.hypotheses[0])

    assert evidence.disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert evidence.metrics == {"throughput": 9.0}
    assert evidence.evaluation_artifact == "eval.json"
    assert evidence.operating_point == "batch=8"
    assert evidence.retention_reason == "best latency"


def test_candidate_evidence_falls_back_to_the_approved_checkpoint() -> None:
    hypothesis = _engine_with_active(
        gate_revalidation_pending=True,
        gate_approved_candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        gate_approved_candidate_metrics={"latency": 3.0},
        gate_approved_candidate_evaluation_artifact="approved.json",
        gate_approved_candidate_operating_point="batch=1",
        gate_approved_candidate_retention_reason="approved by judge",
    ).state.hypotheses[0]
    attempt = _RoundAttemptOutcome(passed=False, feedback=None, implementation=None)

    evidence = _candidate_evidence(attempt, hypothesis)

    assert evidence.disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert evidence.metrics == {"latency": 3.0}
    assert evidence.evaluation_artifact == "approved.json"
    assert evidence.operating_point == "batch=1"
    assert evidence.retention_reason == "approved by judge"

    plain = _engine_with_active().state.hypotheses[0]
    unassessed = _candidate_evidence(attempt, plain)
    assert unassessed.disposition == CandidateDisposition.UNASSESSED.value
    assert unassessed.metrics == {}
    assert unassessed.evaluation_artifact is None


# --------------------------------------------------------------------------
# Retry phases
# --------------------------------------------------------------------------


def _retry_attempt() -> SimpleNamespace:
    return SimpleNamespace(
        hypothesis=SimpleNamespace(
            plan=SimpleNamespace(request_official_evaluation=False),
            feedback=None,
        ),
        engine=None,
        feedback=None,
        retry=1,
        passed=False,
        candidate_ready=False,
        official_evaluation_reason=None,
        single_agent_response=None,
        attempt_judge=None,
    )


def test_single_agent_retry_records_failed_verdict_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(verdict=Verdict.FAIL, feedback="fix the test")
    monkeypatch.setattr(agent_loop, "_run_single_agent_round", lambda *_a, **_k: response)
    persisted: list[object] = []
    monkeypatch.setattr(
        agent_loop, "_persist_round_attempt_hypothesis", lambda _c, a: persisted.append(a)
    )
    attempt = _retry_attempt()
    ctx = _ctx()

    keep_retrying = _run_single_agent_retry(
        ctx,
        _request(),
        cast("Any", SimpleNamespace(records=[])),
        cast("Any", SimpleNamespace(round_number=1)),
        cast("Any", attempt),
    )

    assert keep_retrying is True
    assert attempt.feedback == "fix the test"
    assert attempt.hypothesis.feedback == "fix the test"
    assert attempt.single_agent_response is response
    assert attempt.attempt_judge == JudgeReviewed(Verdict.FAIL)
    assert persisted == [attempt]
    cast("MagicMock", ctx).reselect_gpu.assert_called_once_with()


@pytest.mark.parametrize("due", [False, True])
def test_single_agent_retry_defers_or_runs_the_official_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, due: bool
) -> None:
    response = SimpleNamespace(verdict=Verdict.PASS, feedback="")
    monkeypatch.setattr(agent_loop, "_run_single_agent_round", lambda *_a, **_k: response)
    monkeypatch.setattr(
        agent_loop, "_official_evaluation_reason", lambda **_k: "cadence" if due else None
    )
    monkeypatch.setattr(agent_loop, "_provisional_candidates_since_official", lambda _r: 2)
    gates = MagicMock(return_value="gate-result")
    monkeypatch.setattr(agent_loop, "_run_official_candidate_gates", gates)
    progress_path = tmp_path / "progress.md"
    monkeypatch.setattr(issue_board, "resolve_paths", lambda *_a: (tmp_path, progress_path))
    decisions: list[dict[str, Any]] = []
    monkeypatch.setattr(
        issue_board,
        "append_official_evaluation_decision",
        lambda *a, **kw: decisions.append({"args": a, **kw}),
    )
    attempt = _retry_attempt()
    ctx = _ctx()

    result = _run_single_agent_retry(
        ctx,
        _request(memory_layout="files", official_eval_every=3),
        cast("Any", SimpleNamespace(records=[])),
        cast("Any", SimpleNamespace(round_number=4)),
        cast("Any", attempt),
    )

    assert attempt.candidate_ready is True
    if due:
        assert result == "gate-result"
        assert decisions == []
    else:
        assert result is False
        assert attempt.passed is True
        assert decisions == [
            {
                "args": (progress_path, 4, 1),
                "run": False,
                "reason": "cadence_not_due",
                "official_eval_every": 3,
                "provisional_candidates": 2,
            }
        ]
        gates.assert_not_called()


def _multi_attempt(implementation: object | None) -> SimpleNamespace:
    return SimpleNamespace(
        implementation=implementation,
        retry=2,
        feedback=None,
        hypothesis=SimpleNamespace(plan=SimpleNamespace(request_official_evaluation=False)),
        candidate_ready=False,
        official_evaluation_reason=None,
        passed=False,
    )


def _patch_multi_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, validation_feedback: str | None
) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(issue_board, "resolve_paths", lambda *_a: (tmp_path, tmp_path / "p.md"))
    monkeypatch.setattr(
        agent_loop, "_run_framework_validation_gate", lambda *_a, **_k: validation_feedback
    )
    monkeypatch.setattr(
        agent_loop, "_persist_round_attempt_hypothesis", lambda *_a: calls.append("persist")
    )
    monkeypatch.setattr(agent_loop, "_official_evaluation_reason", lambda **_k: None)
    monkeypatch.setattr(agent_loop, "_provisional_candidates_since_official", lambda _r: 0)
    monkeypatch.setattr(
        issue_board,
        "append_official_evaluation_decision",
        lambda *_a, **_k: calls.append("decision"),
    )
    return calls


def _finish(attempt: SimpleNamespace) -> bool:
    return _finish_multi_agent_pass(
        _ctx(),
        _request(memory_layout="files", official_eval_every=1),
        cast("Any", SimpleNamespace(records=[])),
        cast("Any", SimpleNamespace(round_number=5)),
        cast("Any", attempt),
    )


def test_multi_agent_pass_requires_an_implementer_response() -> None:
    with pytest.raises(RuntimeError, match="passed implementer review has no response"):
        _finish(_multi_attempt(None))


def test_multi_agent_pass_retries_on_validation_feedback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_multi_pass(monkeypatch, tmp_path, validation_feedback="recipe failed")
    attempt = _multi_attempt(
        SimpleNamespace(validation_recipe_artifact="r.json", candidate_disposition=None)
    )

    assert _finish(attempt) is True
    assert attempt.feedback == "recipe failed"
    assert attempt.hypothesis.feedback == "recipe failed"
    assert calls == ["persist"]


def test_multi_agent_pass_checkpoints_pareto_evidence_and_defers_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_multi_pass(monkeypatch, tmp_path, validation_feedback=None)
    implementation = _candidate_row(
        validation_recipe_artifact=None,
        hypothesis_outcome=HypothesisOutcome.SUPPORTED,
    )
    attempt = _multi_attempt(implementation)
    attempt.hypothesis = SimpleNamespace(plan=SimpleNamespace(request_official_evaluation=False))

    assert _finish(attempt) is False

    hypothesis = attempt.hypothesis
    assert hypothesis.gate_approved_candidate_disposition == "pareto_frontier"
    assert hypothesis.gate_approved_candidate_metrics == {"throughput": 9.0}
    assert hypothesis.gate_approved_candidate_evaluation_artifact == "eval.json"
    assert hypothesis.gate_approved_candidate_operating_point == "batch=8"
    assert hypothesis.gate_approved_candidate_retention_reason == "best latency"
    assert attempt.candidate_ready is True
    assert attempt.passed is True
    assert calls == ["persist", "decision"]


def test_multi_agent_pass_checkpoints_the_reported_metric_before_official_gates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_multi_pass(monkeypatch, tmp_path, validation_feedback=None)
    monkeypatch.setattr(agent_loop, "_official_evaluation_reason", lambda **_k: "cadence")
    monkeypatch.setattr(agent_loop, "_run_official_candidate_gates", lambda *_a: "gates")
    implementation = _candidate_row(
        candidate_disposition=CandidateDisposition.UNASSESSED,
        validation_recipe_artifact=None,
        hypothesis_outcome=HypothesisOutcome.NOMINATED,
        perf_metric=12.5,
        perf_unit="tok/s",
        metrics={"throughput": 12.5},
        evaluation_artifact="official.json",
    )
    attempt = _multi_attempt(implementation)

    assert _finish(attempt) == "gates"

    assert attempt.hypothesis.gate_approved_perf_metric == 12.5
    assert attempt.hypothesis.gate_approved_perf_unit == "tok/s"
    assert attempt.hypothesis.gate_approved_metrics == {"throughput": 12.5}
    assert attempt.hypothesis.gate_approved_evaluation_artifact == "official.json"
    assert calls == ["persist"]


# --------------------------------------------------------------------------
# Attempt loop
# --------------------------------------------------------------------------


def test_attempt_loop_refuses_to_replay_exhausted_durable_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(issue_board, "resolve_paths", lambda *_a: (tmp_path, tmp_path / "p.md"))
    monkeypatch.setattr(issue_board, "next_implementer_attempt", lambda *_a: 4)

    with pytest.raises(RuntimeError, match=r"already persisted 3 implementer attempts"):
        _run_agent_attempts(
            _ctx(),
            _request(memory_layout="files", max_retries_per_round=3),
            _engine_with_active(),
            cast("Any", SimpleNamespace(records=[])),
            cast("Any", SimpleNamespace(round_number=2)),
        )


def test_attempt_loop_resumes_at_the_next_durable_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(issue_board, "resolve_paths", lambda *_a: (tmp_path, tmp_path / "p.md"))
    monkeypatch.setattr(issue_board, "next_implementer_attempt", lambda *_a: 2)
    seen: list[int] = []

    def retry(
        _ctx: object,
        _request: object,
        _history: object,
        _progress: object,
        attempt: SimpleNamespace,
    ) -> bool:
        seen.append(attempt.retry)
        attempt.passed = True
        return False

    monkeypatch.setattr(agent_loop, "_run_single_agent_retry", retry)
    ctx = _ctx()

    _engine, outcome = _run_agent_attempts(
        ctx,
        _request(memory_layout="files", max_retries_per_round=3, inner_loop="single-agent"),
        _engine_with_active(),
        cast("Any", SimpleNamespace(records=[])),
        cast("Any", SimpleNamespace(round_number=2)),
    )

    assert seen == [2]
    assert outcome.passed is True
    assert (outcome.retry, outcome.retry_limit, outcome.round_number) == (2, 3, 2)
    logged = [call.args[0] for call in cast("MagicMock", ctx).lprint.call_args_list]
    assert any("continues at durable attempt 2/3" in line for line in logged)


def test_judge_requires_an_active_hypothesis() -> None:
    engine = HypothesisEngine.create(AgentRunState(), config=None)
    with pytest.raises(RuntimeError, match="cannot run a judge without an active hypothesis"):
        _run_judge(
            _ctx(),
            _request(),
            engine,
            cast("Any", SimpleNamespace(round_number=1)),
            cast("Any", SimpleNamespace()),
        )


def test_rollback_to_a_failed_child_reverts_to_the_pre_hypothesis_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persisted: list[Hypothesis] = []

    def persist(
        _ctx: object, _store: object, state: AgentRunState, hypothesis: Hypothesis, **_kw: object
    ) -> AgentRunState:
        persisted.append(hypothesis)
        return state

    monkeypatch.setattr(agent_loop, "persist_active_hypothesis", persist)
    target = SimpleNamespace(round_number=1, commit="a" * 40)
    history = SimpleNamespace(
        records=[target],
        resolve_rollback_commit=lambda *_a: ("e" * 40, 2),
    )
    ctx = MagicMock()
    ctx.workspace = tmp_path
    ctx.git.checkout_tree.return_value = True

    _apply_agent_rollback(
        cast("LoopContext", ctx),
        _request(memory_layout="files"),
        _plan("x", revert_to_round=1),
        cast("Any", history),
        _engine_with_active(),
    )

    message = ctx.lprint.call_args.args[0]
    assert "failed round 2 (eeeeeeee), based on parent round 1." in message
    (saved,) = persisted
    assert saved.revert_applied is True
    assert saved.revert_commit == saved.parent_commit == "e" * 40
