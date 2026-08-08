"""Tests for vibesys.loops.agent — orchestrator-driven build loop."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest

from vibesys.agents import AgentRunner
from vibesys.domains.base import DomainName
from vibesys.errors import ConfigurationError
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.loop import (
    _ActiveHypothesis,
    _backfill_revert_commit,
    _candidate_evidence_is_fresh,
    _invoke_read_only_role,
    _missing_implementer_response,
    _noise_aware_dominates,
    _official_evaluation_reason,
    _pareto_archive_summary,
    _pareto_frontier_records,
    _provisional_candidates_since_official,
    _review_due,
    _run_framework_validation_gate,
    _terminal_workspace_notice,
    run_agent_loop,
)
from vibesys.loops.evolve.population import Objective
from vibesys.profilers import ProfilerKind, ProfilerPreflightResult
from vibesys.prompts import PROMPTS_DIR
from vibesys.run import GitTracker
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SkillResourceSelection,
    ValidationRecipe,
    ValidationRecipeArtifact,
    Verdict,
)
from vs_loop_state.agent import RoundRecord

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def test_missing_implementer_response_fails_closed():  # noqa: ANN201  # tracked: #288
    response = _missing_implementer_response()

    assert response.hypothesis_outcome is HypothesisOutcome.INCONCLUSIVE
    assert response.perf_metric is None
    assert response.evaluation_artifact is None
    assert "schema-valid" in response.next_step


def test_legacy_active_hypothesis_backfills_framework_revert_commit():  # noqa: ANN201  # tracked: #288
    state = _ActiveHypothesis(
        plan=OrchestratorPlan(
            task="restore parent",
            pass_criteria="review",  # noqa: S106  # tracked: #288
            revert_to_round=28,
            reasoning="resume an older run",
        ),
        started_round=34,
        parent_round=28,
        revert_applied=True,
    )
    records = [
        RoundRecord(
            round_number=28,
            commit="a" * 40,
            perf_metric=None,
            perf_unit=None,
            passed=False,
        )
    ]

    assert _backfill_revert_commit(state, records) is True
    assert state.revert_commit == "a" * 40
    assert _backfill_revert_commit(state, records) is False


# RoundRecord's persistence and rollback resolution (RoundHistory) now live
# in libs/vs-loop-state; see its own tests
# (libs/vs-loop-state/tests/test_vs_loop_state_agent.py) for that coverage,
# including the failed-child, implementation-failed, and distant-rollback
# cases previously duplicated here.


@pytest.fixture
def ref_file(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """Create a reference *file* (not dir) + an OBJECTIVE.md sibling.

    Using a single file avoids the model-weight lookup that a reference
    directory triggers, which keeps these tests independent of HF cache
    state.
    """
    model_dir = tmp_path / "input_model"
    model_dir.mkdir()
    ref = model_dir / "ref.py"
    ref.write_text("def predict(x): return x * 2\n")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n")
    (model_dir / "vibesys.input.toml").write_text(
        """
version = 1

[agent]
domain = "llm-serving"

[accuracy]
command = ["uv", "run", "python", "accuracy_checker/checker.py"]

[benchmark]
command = ["uv", "run", "python", "benchmark/benchmark.py"]
""".lstrip()
    )
    return str(ref)


def _make_orchestrate_runner(  # noqa: ANN202, PLR0913  # tracked: #288
    *,
    pre_decisions: list[PreRoundDecision] | None = None,
    plans: list[OrchestratorPlan] | None = None,
    implementer_outcomes: list[HypothesisOutcome] | None = None,
    judge_verdicts: list[str] | None = None,
    profiler_responses: list[ProfilerSummary] | None = None,
    implementer_perf_metrics: list[float | None] | None = None,
    implementer_skill_updates: list[list[SkillResourceSelection]] | None = None,
    implementer_validation_artifacts: list[str | None] | None = None,
    implementer_next_steps: list[str] | None = None,
):
    """Build a MagicMock AgentRunner whose invoke() returns scripted responses.

    Arguments are consumed-in-order queues keyed by the agent kind / response
    class. Defaults: when the plan queue is exhausted, the harness returns a
    permissive no-op plan and lets the loop's ``max_rounds`` bound the test.
    Judge verdicts default to pass; the profiler is not called.
    """
    pre_q = list(pre_decisions or [])
    plan_q = list(plans or [])
    outcome_q = list(implementer_outcomes or [])
    judge_q = list(judge_verdicts or [])
    prof_q = list(profiler_responses or [])
    impl_perf_q = list(implementer_perf_metrics or [])
    impl_skill_q = list(implementer_skill_updates or [])
    impl_validation_q = list(implementer_validation_artifacts or [])
    impl_next_step_q = list(implementer_next_steps or [])
    counters = {"impl": 0, "judge": 0, "orch_pre": 0, "orch_plan": 0, "prof": 0}

    runner = MagicMock(spec=AgentRunner)
    runner.backend_name = "deepagents"

    def _invoke(*, kind, response_cls, fallback_factory, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001, PLR0911  # tracked: #288
        if kind == "orchestrator" and response_cls is PreRoundDecision:
            counters["orch_pre"] += 1
            if pre_q:
                return pre_q.pop(0)
            return PreRoundDecision(need_profile=False, profile_focus="", reasoning="default skip")
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            counters["orch_plan"] += 1
            if plan_q:
                return plan_q.pop(0)
            return OrchestratorPlan(
                task="noop (harness default)",
                pass_criteria="no criteria",  # noqa: S106  # tracked: #288
                reasoning="default noop plan — the loop's max_rounds bounds the test",
            )
        if kind == "implementer":
            counters["impl"] += 1
            outcome = outcome_q.pop(0) if outcome_q else HypothesisOutcome.NOMINATED
            perf_metric = impl_perf_q.pop(0) if impl_perf_q else None
            skill_updates = impl_skill_q.pop(0) if impl_skill_q else []
            validation_artifact = impl_validation_q.pop(0) if impl_validation_q else None
            next_step = (
                impl_next_step_q.pop(0)
                if impl_next_step_q
                else (
                    "continue experiment"
                    if outcome
                    in {
                        HypothesisOutcome.CONTINUE,
                        HypothesisOutcome.IMPLEMENTATION_FAILED,
                        HypothesisOutcome.INCONCLUSIVE,
                    }
                    else ""
                )
            )
            return ImplementerResponse(
                summary="Done.",
                expected_behavior="ok",
                hypothesis_outcome=outcome,
                evidence="targeted evidence",
                next_step=next_step,
                perf_metric=perf_metric,
                perf_unit="tok/s" if perf_metric is not None else None,
                metrics={"aggregate_throughput": perf_metric, "p99_latency_ms": 87.0}
                if perf_metric is not None
                else {},
                evaluation_artifact="benchmark/summary.json" if perf_metric is not None else None,
                skill_context_updates=skill_updates,
                validation_recipe_artifact=validation_artifact,
            )
        if kind == "judge":
            idx = counters["judge"]
            counters["judge"] += 1
            v = judge_q[idx] if idx < len(judge_q) else "pass"
            return JudgeResponse(
                analysis="ok",
                feedback="" if v == "pass" else "needs work",
                verdict=Verdict.PASS if v == "pass" else Verdict.FAIL,
            )
        if kind == "profiler":
            counters["prof"] += 1
            if prof_q:
                return prof_q.pop(0)
            return ProfilerSummary(
                analysis="ok",
                bottlenecks="none",
                suggestions="none",
            )
        raise AssertionError(f"unexpected kind: {kind}, response_cls={response_cls}")  # noqa: TRY003  # tracked: #288

    runner.invoke.side_effect = _invoke
    runner.counters = counters  # test introspection
    return runner


def _invoke_orchestrate(tmp_path, ref_file, runner, **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
    """Shared plumbing: patch context globals, run the loop, return result."""
    accuracy_gate_results = kwargs.pop("_accuracy_gate_results", None)
    defaults = dict(  # noqa: C408  # tracked: #288
        config={"model": {"name": "claude-sonnet-4-6"}},
        exp_name="test-orch",
        input_path=str(Path(ref_file).parent),
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
        objective="Maximize tok/s throughput.",
        max_rounds=5,
        max_retries_per_round=2,
        domain=DomainName.LLM_SERVING,
    )
    defaults.update(kwargs)
    with (
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.backends.cuda.LocalShellBackend"),
        patch("vibesys.context.build_agent_runner", return_value=runner),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        patch(
            "vibesys.loops.agent.loop._run_framework_accuracy_gate",
            side_effect=accuracy_gate_results,
            return_value=None,
        ),
    ):
        return run_agent_loop(**defaults)  # pyright: ignore[reportArgumentType]  # tracked: #297


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


def test_validation_recipe_rejects_non_workspace_inputs():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="workspace-relative"):
        ValidationRecipe(
            name="focused-tests",
            command="uv run pytest -q tests/test_server.py",
            input_paths=["../outside.py"],
            purpose="Exercise the local server contract.",
        )


def test_validation_recipe_artifact_rejects_invented_top_level_shape():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="recipes"):
        ValidationRecipeArtifact.model_validate(
            {
                "version": 1,
                "checks": [
                    {
                        "name": "focused-tests",
                        "command": "uv run pytest -q test_server.py",
                    }
                ],
            }
        )


def test_issue_board_publishes_authoritative_validation_recipe_schema(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"

    path = issue_board.write_validation_recipe_schema(progress)
    schema = json.loads(path.read_text())

    assert path == progress / "validation" / "recipe-schema.json"
    assert schema["properties"]["version"]["const"] == 1
    assert schema["properties"]["recipes"]["minItems"] == 1
    assert schema["properties"]["recipes"]["maxItems"] == 8
    assert schema["examples"][0]["recipes"][0]["name"] == "focused-tests"


def test_read_only_role_reverts_workspace_mutations_and_keeps_response():  # noqa: ANN201  # tracked: #288
    ctx = MagicMock()
    ctx.git.current_sha.return_value = "a" * 40
    ctx.git.pending_changes.side_effect = [["roadmap/index.md", "scratch.txt"], []]
    ctx.git.checkout_tree.return_value = True
    expected = OrchestratorPlan(
        task="next",
        pass_criteria="passes",  # noqa: S106  # tracked: #288
        reasoning="evidence supports next",  # noqa: RUF100, S106  # tracked: #288
    )
    ctx.invoke.return_value = expected

    result = _invoke_read_only_role(
        ctx,
        role="orchestrator",
        checkpoint_label="round-2-plan-input",
        kind="orchestrator",
        system_prompt="plan",
        user_prompt="return JSON",
        response_cls=OrchestratorPlan,
        fallback_factory=lambda: expected,
    )

    assert result is expected
    ctx.snapshot_workspace.assert_called_once_with("round-2-plan-input")
    ctx.git.checkout_tree.assert_called_once_with("a" * 40, clean=True)
    ctx.lprint.assert_called_once()


def test_read_only_role_does_not_restore_clean_turn():  # noqa: ANN201  # tracked: #288
    ctx = MagicMock()
    ctx.git.current_sha.return_value = "b" * 40
    ctx.git.pending_changes.return_value = []
    expected = JudgeResponse(analysis="clean", feedback="", verdict=Verdict.PASS)
    ctx.invoke.return_value = expected

    result = _invoke_read_only_role(
        ctx,
        role="judge",
        checkpoint_label="round-2-judge-input",
        kind="judge",
        system_prompt="judge",
        user_prompt="return JSON",
        response_cls=JudgeResponse,
        fallback_factory=lambda: expected,
    )

    assert result is expected
    ctx.git.checkout_tree.assert_not_called()
    ctx.lprint.assert_not_called()


def test_read_only_role_preserves_allowed_roadmap_and_reverts_other_writes(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    experiment = tmp_path / "experiment"
    workspace = experiment / "workspace"
    roadmap = workspace / "roadmap" / "index.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text("initial roadmap\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=experiment, check=True)  # noqa: S607  # tracked: #288
    tracker = GitTracker(workspace, log=lambda _message: None)
    tracker.init(existing=False)

    expected = OrchestratorPlan(
        task="next",
        pass_criteria="passes",  # noqa: S106  # tracked: #288
        reasoning="evidence supports next",  # noqa: RUF100, S106  # tracked: #288
    )

    def invoke(**_kwargs):  # noqa: ANN003, ANN202  # tracked: #288
        roadmap.write_text("updated roadmap\n")
        (workspace / "main.py").write_text("unauthorized candidate edit\n")
        return expected

    logs: list[str] = []
    ctx = SimpleNamespace(
        workspace=workspace,
        git=tracker,
        invoke=invoke,
        snapshot_workspace=tracker.snapshot,
        lprint=logs.append,
    )

    result = _invoke_read_only_role(
        ctx,  # pyright: ignore[reportArgumentType]  # tracked: #297
        role="orchestrator",
        checkpoint_label="round-2-plan-input",
        allowed_workspace_paths=("roadmap/index.md",),
        kind="orchestrator",
        system_prompt="plan",
        user_prompt="return JSON",
        response_cls=OrchestratorPlan,
        fallback_factory=lambda: expected,
    )

    assert result is expected
    assert roadmap.read_text() == "updated roadmap\n"
    assert not (workspace / "main.py").exists()
    assert tracker.pending_changes() == ["roadmap/index.md"]
    assert any("main.py" in line for line in logs)


def test_read_only_role_preserves_allowed_directory_and_reverts_candidate_edits(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    experiment = tmp_path / "experiment"
    workspace = experiment / "workspace"
    workspace.mkdir(parents=True)
    main = workspace / "main.py"
    main.write_text("accepted candidate\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=experiment, check=True)  # noqa: S607  # tracked: #288
    tracker = GitTracker(workspace, log=lambda _message: None)
    tracker.init(existing=False)

    expected = ProfilerSummary(analysis="done", bottlenecks="b", suggestions="s")

    def invoke(**_kwargs):  # noqa: ANN003, ANN202  # tracked: #288
        main.write_text("profiler instrumentation\n")
        artifact = workspace / "progress" / "profiles" / "round-0002" / "summary.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"captured": true}\n')
        return expected

    logs: list[str] = []
    ctx = SimpleNamespace(
        workspace=workspace,
        git=tracker,
        invoke=invoke,
        snapshot_workspace=tracker.snapshot,
        lprint=logs.append,
    )

    result = _invoke_read_only_role(
        ctx,  # pyright: ignore[reportArgumentType]  # tracked: #297
        role="profiler",
        checkpoint_label="round-2-profiler-input",
        allowed_workspace_paths=("progress/profiles/round-0002",),
        kind="profiler",
        system_prompt="profile",
        user_prompt="return JSON",
        response_cls=ProfilerSummary,
        fallback_factory=lambda: expected,
    )

    assert result is expected
    assert main.read_text() == "accepted candidate\n"
    assert (workspace / "progress" / "profiles" / "round-0002" / "summary.json").is_file()
    assert tracker.pending_changes() == ["progress/profiles/round-0002/summary.json"]
    assert any("main.py" in line for line in logs)


def test_pre_round_decision_accepts_booleans():  # noqa: ANN201  # tracked: #288
    d = PreRoundDecision(need_profile=True, profile_focus="decode kernels", reasoning="ok")
    assert d.need_profile is True
    assert d.profile_focus == "decode kernels"


def test_orchestrator_plan_revert_round_optional():  # noqa: ANN201  # tracked: #288
    p = OrchestratorPlan(
        task="redo",
        pass_criteria="passes tests",  # noqa: S106  # tracked: #288
        revert_to_round=3,
        reasoning="step back",
    )
    assert p.revert_to_round == 3


def test_official_evaluation_cadence_counts_candidate_checkpoints_not_rounds():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(1, "a", None, None, False, reviewed=False, hypothesis_outcome="continue"),  # noqa: FBT003  # tracked: #288
        RoundRecord(2, "b", None, None, True, reviewed=True, hypothesis_outcome="proven"),  # noqa: FBT003  # tracked: #288
        RoundRecord(3, "c", None, None, False, reviewed=True, hypothesis_outcome="rejected"),  # noqa: FBT003  # tracked: #288
        RoundRecord(4, "d", None, None, True, reviewed=True, hypothesis_outcome="proven"),  # noqa: FBT003  # tracked: #288
    ]

    assert _provisional_candidates_since_official(records) == 2
    assert (
        _official_evaluation_reason(
            records=records,
            round_number=5,
            max_rounds=20,
            official_eval_every=3,
            requested=False,
            candidate_ready=True,
        )
        == "cadence"
    )


def test_frontier_candidate_forces_review_outside_sparse_cadence():  # noqa: ANN201  # tracked: #288
    assert _review_due(
        round_number=5,
        max_rounds=20,
        judge_every=3,
        outcome=HypothesisOutcome.DISPROVEN,
        candidate_evidence_fresh=True,
    )


def test_measured_candidate_forces_review_even_when_disposition_is_downgraded():  # noqa: ANN201  # tracked: #288
    assert _review_due(
        round_number=5,
        max_rounds=20,
        judge_every=3,
        outcome=HypothesisOutcome.INCONCLUSIVE,
        candidate_evidence_fresh=True,
    )


def test_reused_candidate_evidence_does_not_bypass_sparse_review():  # noqa: ANN201  # tracked: #288
    metrics = {"throughput": 6205.0, "latency": 10660.0}
    record = RoundRecord(
        round_number=75,
        commit="a" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=False,
        candidate_disposition=CandidateDisposition.PREREQUISITE.value,
        candidate_metrics=metrics,
        candidate_evaluation_artifact="h37-round77-controller-raw.json",
        candidate_operating_point="concurrency 512",
    )
    implementation = ImplementerResponse(
        summary="Rebuilt derived reports without running a benchmark.",
        expected_behavior="The retained raw row is unchanged.",
        hypothesis_outcome=HypothesisOutcome.INCONCLUSIVE,
        candidate_disposition=CandidateDisposition.PREREQUISITE,
        candidate_metrics=metrics,
        candidate_evaluation_artifact="h37-round77-controller-raw.json",
        candidate_operating_point="concurrency 512",
    )

    assert not _candidate_evidence_is_fresh(implementation, [record])
    assert not _review_due(
        round_number=76,
        max_rounds=200,
        judge_every=3,
        outcome=implementation.hypothesis_outcome,
        candidate_evidence_fresh=False,
    )


def test_changed_candidate_row_is_fresh_even_when_artifact_name_is_reused():  # noqa: ANN201  # tracked: #288
    record = RoundRecord(
        round_number=4,
        commit="a" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=False,
        candidate_metrics={"throughput": 100.0},
        candidate_evaluation_artifact="candidate.json",
        candidate_operating_point="load 8",
    )
    implementation = ImplementerResponse(
        summary="Measured a changed row.",
        expected_behavior="Throughput changes.",
        candidate_metrics={"throughput": 110.0},
        candidate_evaluation_artifact="candidate.json",
        candidate_operating_point="load 8",
    )

    assert _candidate_evidence_is_fresh(implementation, [record])


def test_official_evaluation_cadence_counts_reviewed_frontier_tradeoff():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(
            2,
            "b",
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_outcome="disproven",
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={"throughput": 120.0, "latency": 90.0},
        )
    ]

    assert _provisional_candidates_since_official(records) == 1


def test_noise_aware_dominance_preserves_sub_noise_alternatives():  # noqa: ANN201  # tracked: #288
    objectives = [Objective("throughput", "max"), Objective("latency", "min")]

    assert not _noise_aware_dominates(
        {"throughput": 102.0, "latency": 101.0},
        {"throughput": 100.0, "latency": 100.0},
        objectives,
        relative_noise=0.03,
    )
    assert _noise_aware_dominates(
        {"throughput": 110.0, "latency": 101.0},
        {"throughput": 100.0, "latency": 100.0},
        objectives,
        relative_noise=0.03,
    )


def test_pareto_frontier_keeps_throughput_latency_tradeoff_and_drops_dominated_point():  # noqa: ANN201  # tracked: #288
    objectives = [Objective("throughput", "max"), Objective("latency", "min")]

    def candidate(round_number: int, throughput: float, latency: float) -> RoundRecord:
        return RoundRecord(
            round_number,
            str(round_number) * 40,
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_metrics={"throughput": throughput, "latency": latency},
            candidate_evaluation_artifact=f"round-{round_number}.json",
            candidate_operating_point="concurrency=128",
        )

    latency_parent = candidate(1, 100.0, 80.0)
    throughput_parent = candidate(2, 140.0, 100.0)
    dominated = candidate(3, 90.0, 110.0)

    frontier = _pareto_frontier_records([latency_parent, throughput_parent, dominated], objectives)

    assert [record.round_number for record in frontier] == [1, 2]


def test_live_archive_rejects_stale_frontier_claim_for_dominated_candidate():  # noqa: ANN201  # tracked: #288
    from vibesys.loops.agent.loop import _pareto_archive_conflict  # noqa: PLC0415  # tracked: #288

    objectives = [Objective("throughput", "max"), Objective("latency", "min")]
    trusted = RoundRecord(
        61,
        "a" * 40,
        None,
        None,
        True,  # noqa: FBT003  # tracked: #288
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 8795.8, "latency": 7724.0},
    )

    conflict = _pareto_archive_conflict(
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
        candidate_metrics={"throughput": 7258.5, "latency": 9601.6},
        records=[trusted],
        objectives=objectives,
    )

    assert conflict is not None
    assert "round 61" in conflict
    assert "frozen into the hypothesis plan" in conflict


def test_live_archive_preserves_real_throughput_latency_tradeoff():  # noqa: ANN201  # tracked: #288
    from vibesys.loops.agent.loop import _pareto_archive_conflict  # noqa: PLC0415  # tracked: #288

    objectives = [Objective("throughput", "max"), Objective("latency", "min")]
    trusted = RoundRecord(
        6,
        "a" * 40,
        None,
        None,
        True,  # noqa: FBT003  # tracked: #288
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 100.0, "latency": 80.0},
    )

    assert (
        _pareto_archive_conflict(
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
            candidate_metrics={"throughput": 140.0, "latency": 100.0},
            records=[trusted],
            objectives=objectives,
        )
        is None
    )


def test_pareto_archive_distinguishes_trusted_and_pending_candidates():  # noqa: ANN201  # tracked: #288
    objectives = [Objective("throughput", "max"), Objective("latency", "min")]
    trusted = RoundRecord(
        49,
        "a" * 40,
        None,
        None,
        True,  # noqa: FBT003  # tracked: #288
        reviewed=True,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 5307.2, "latency": 3289.7},
        candidate_evaluation_artifact="h31.json",
        candidate_operating_point="concurrency=128",
    )
    pending = RoundRecord(
        51,
        "b" * 40,
        None,
        None,
        False,  # noqa: FBT003  # tracked: #288
        reviewed=False,
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 6827.7, "latency": 3628.7},
        candidate_evaluation_artifact="h33.json",
        candidate_operating_point="concurrency=192",
        candidate_retention_reason="higher-throughput tradeoff",
    )

    summary = _pareto_archive_summary([trusted, pending], objectives)

    assert "Trusted frontier parents" in summary
    assert "round 49" in summary
    assert "awaiting independent review" in summary
    assert "round 51" in summary


def test_official_evaluation_cadence_resets_at_verified_checkpoint():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(
            1,
            "a",
            10.0,
            "tok/s",
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_outcome="proven",
            official_evaluation=True,
            official_evaluation_reason="orchestrator_request",
        ),
        RoundRecord(2, "b", None, None, True, reviewed=True, hypothesis_outcome="proven"),  # noqa: FBT003  # tracked: #288
    ]

    assert _provisional_candidates_since_official(records) == 1
    assert (
        _official_evaluation_reason(
            records=records,
            round_number=3,
            max_rounds=20,
            official_eval_every=3,
            requested=False,
            candidate_ready=True,
        )
        is None
    )


def test_terminal_workspace_notice_points_designer_to_hypothesis_parent():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(28, "a" * 40, None, None, False),  # noqa: FBT003  # tracked: #288
        RoundRecord(
            29,
            "b" * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="bad-scheduler",
            hypothesis_outcome="rejected",
        ),
        RoundRecord(
            30,
            "c" * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=False,
            hypothesis_id="bad-scheduler",
            hypothesis_outcome="disproven",
            hypothesis_parent_round=28,
        ),
    ]

    notice = _terminal_workspace_notice(records)

    assert notice is not None
    assert "workspace edits are still present" in notice
    assert "recorded pre-hypothesis parent is round 28" in notice
    assert "revert_to_round=28" in notice


def test_terminal_workspace_notice_preserves_pareto_tradeoff_commit():  # noqa: ANN201  # tracked: #288
    record = RoundRecord(
        51,
        "b" * 40,
        None,
        None,
        False,  # noqa: FBT003  # tracked: #288
        reviewed=False,
        hypothesis_id="capacity-192",
        hypothesis_outcome="disproven",
        candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
        candidate_metrics={"throughput": 6827.7, "latency": 3628.7},
    )

    notice = _terminal_workspace_notice([record])

    assert notice is not None
    assert "Preserve commit" in notice
    assert "awaiting independent review" in notice
    assert "do not erase a credible throughput/latency tradeoff" in notice


def test_terminal_workspace_notice_preserves_credible_continuation_checkpoint():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(28, "a" * 40, None, None, False),  # noqa: FBT003  # tracked: #288
        RoundRecord(
            34,
            "b" * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=False,
            hypothesis_id="host-autopsy",
            hypothesis_outcome="continue",
            hypothesis_parent_round=28,
        ),
        RoundRecord(
            35,
            "c" * 40,
            None,
            None,
            False,  # noqa: FBT003  # tracked: #288
            reviewed=False,
            hypothesis_id="host-autopsy",
            hypothesis_outcome="continue",
            hypothesis_parent_round=28,
        ),
        RoundRecord(
            36,
            "d" * 40,
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="host-autopsy",
            hypothesis_outcome="disproven",
            hypothesis_parent_round=28,
        ),
    ]

    notice = _terminal_workspace_notice(records)

    assert notice is not None
    assert "recorded pre-hypothesis parent is round 28" in notice
    assert "most recent earlier nonterminal checkpoint is round 35" in notice
    assert "preserve that checkpoint instead of discarding prior gains" in notice
    assert "An older implementation cannot be required to reproduce" in notice


def test_terminal_workspace_notice_keeps_original_parent_after_same_id_reproposal():  # noqa: ANN201  # tracked: #288
    records = [
        RoundRecord(60, "a" * 40, None, None, True),  # noqa: FBT003  # tracked: #288
        RoundRecord(
            61,
            "b" * 40,
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="quantum-decode",
            hypothesis_outcome="implementation_failed",
            hypothesis_parent_round=60,
        ),
        RoundRecord(
            62,
            "c" * 40,
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="quantum-decode",
            hypothesis_outcome="blocked",
            hypothesis_parent_round=60,
        ),
        RoundRecord(
            63,
            "d" * 40,
            None,
            None,
            True,  # noqa: FBT003  # tracked: #288
            reviewed=True,
            hypothesis_id="quantum-decode",
            hypothesis_outcome="inconclusive",
            hypothesis_parent_round=62,
        ),
    ]

    notice = _terminal_workspace_notice(records)

    assert notice is not None
    assert "recorded pre-hypothesis parent is round 60" in notice
    assert "revert_to_round=60" in notice
    assert "recorded pre-hypothesis parent is round 62" not in notice


def test_profiler_summary_perf_metric_optional():  # noqa: ANN201  # tracked: #288
    p = ProfilerSummary(analysis="a", bottlenecks="b", suggestions="s")
    assert p.perf_metric is None
    p2 = ProfilerSummary(
        analysis="a",
        bottlenecks="b",
        suggestions="s",
        perf_metric=12.5,
        perf_unit="tok/s",
    )
    assert p2.perf_metric == 12.5
    assert p2.perf_unit == "tok/s"


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------


def test_progress_writes_orchestrator_plan(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    plan = OrchestratorPlan(
        task="Build FastAPI server",
        pass_criteria="/health returns 200",  # noqa: S106  # tracked: #288
        reasoning="Round 1 cold start",
        expected_effect="Forecast 1.3x to 1.6x throughput",
        minimum_acceptance_criteria="Retain at >=1.15x with no latency regression",
    )
    issue_board.append_orchestrator_plan(progress, 1, plan)
    text = progress.read_text()
    assert "Round 1 — Orchestrator (plan)" in text
    assert "Build FastAPI server" in text
    assert "/health returns 200" in text
    assert "Forecast 1.3x to 1.6x throughput" in text
    assert "Retain at >=1.15x with no latency regression" in text


@pytest.mark.parametrize(
    ("progress_name", "artifact_root"),
    [("progress", "progress"), ("progress.md", "progress-artifacts")],
)
def test_progress_writes_typed_role_handoffs_atomically(tmp_path, progress_name, artifact_root):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / progress_name
    plan = OrchestratorPlan(
        hypothesis_id="transport-boundary",
        task="Replace the request-local queue.",
        pass_criteria="The direct path activates.",  # noqa: S106  # tracked: #288
        reasoning="The retained profile leaves a service residual.",
    )
    implementation = ImplementerResponse(
        summary="Implemented direct delivery.",
        expected_behavior="No request-local queue wakeup.",
        evidence="Untrusted implementer claim.",
    )

    plan_path = issue_board.write_plan_artifact(progress, 12, plan)
    evidence_path = issue_board.write_implementer_artifact(progress, 12, 2, implementation)

    assert plan_path == tmp_path / artifact_root / "plans" / "round-0012.json"
    assert evidence_path == (
        tmp_path / artifact_root / "evidence" / "round-0012-attempt-02-implementer.json"
    )
    assert OrchestratorPlan.model_validate_json(plan_path.read_text()) == plan
    assert ImplementerResponse.model_validate_json(evidence_path.read_text()) == implementation
    assert not list((tmp_path / artifact_root).rglob(".*.tmp*"))


def test_persisted_implementer_attempts_define_resume_boundary(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"
    implementation = ImplementerResponse(
        summary="Retained the first target run.",
        expected_behavior="A resumed round must not overwrite it.",
    )
    first = issue_board.write_implementer_artifact(progress, 8, 1, implementation)
    second = issue_board.write_implementer_artifact(progress, 8, 2, implementation)

    assert issue_board.implementer_artifact_paths(progress, 8) == [first, second]
    assert issue_board.next_implementer_attempt(progress, 8) == 3
    assert issue_board.next_implementer_attempt(progress, 9) == 1


def test_agent_memory_paths_distinguish_files_from_directories(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    workspace = tmp_path / "workspace"
    directory = workspace / "progress"
    artifact = directory / "plans" / "round-0012.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n")

    assert issue_board.display_path(directory, workspace) == "progress/"
    assert issue_board.display_path(artifact, workspace) == "progress/plans/round-0012.json"


def test_progress_replaces_interrupted_stage_instead_of_duplicating_it(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"
    issue_board.append_pre_round_decision(
        progress,
        7,
        PreRoundDecision(
            need_profile=True,
            profile_focus="stale focus",
            reasoning="stale decision",
        ),
    )
    issue_board.append_orchestrator_plan(
        progress,
        7,
        OrchestratorPlan(
            task="Keep this plan",
            pass_criteria="plan remains",  # noqa: S106  # tracked: #288
            reasoning="retained plan",
        ),
    )

    issue_board.append_pre_round_decision(
        progress,
        7,
        PreRoundDecision(
            need_profile=False,
            profile_focus="",
            reasoning="resumed decision",
        ),
    )

    text = (progress / "round-0007.md").read_text()
    assert text.count("## Round 7 — Orchestrator (pre-round)") == 1
    assert "resumed decision" in text
    assert "stale decision" not in text
    assert text.count("## Round 7 — Orchestrator (plan)") == 1
    assert "Keep this plan" in text


def test_progress_replacement_preserves_operator_recovery_section(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress"
    issue_board.append_hypothesis_continuation(
        progress,
        7,
        plan=OrchestratorPlan(
            hypothesis_id="transport",
            hypothesis="remove queue fanout",
            task="stale initial implementation task",
            pass_criteria="source is recoverable",  # noqa: S106  # tracked: #288
            reasoning="continue interrupted work",
        ),
        started_round=6,
        continuation_step="recover exact source",
    )
    round_file = progress / "round-0007.md"
    with round_file.open("a") as document:
        document.write(
            "## Operator recovery evidence\n"
            "Exact measured bytes are retained at `recovery/source.py`.\n\n"
        )

    issue_board.append_hypothesis_continuation(
        progress,
        7,
        plan=OrchestratorPlan(
            hypothesis_id="transport",
            hypothesis="remove queue fanout",
            task="stale initial implementation task",
            pass_criteria="source is recoverable",  # noqa: S106  # tracked: #288
            reasoning="resume interrupted work",
        ),
        started_round=6,
        continuation_step="verify recovered source",
    )

    text = round_file.read_text()
    assert text.count("## Round 7 — Active hypothesis continuation") == 1
    assert "### Current continuation delta" in text
    assert "verify recovered source" in text
    assert "recover exact source" not in text
    assert "stale initial implementation task" not in text
    assert text.count("## Operator recovery evidence") == 1
    assert "Exact measured bytes are retained" in text


def test_progress_preserves_distinct_attempts_but_replaces_same_attempt(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    issue_board.append_implementer(
        progress,
        3,
        1,
        ImplementerResponse(summary="interrupted", expected_behavior="old"),
    )
    issue_board.append_implementer(
        progress,
        3,
        1,
        ImplementerResponse(summary="resumed", expected_behavior="new"),
    )
    issue_board.append_implementer(
        progress,
        3,
        2,
        ImplementerResponse(summary="retry", expected_behavior="newer"),
    )

    text = progress.read_text()
    assert text.count("## Round 3 — Implementer (attempt 1)") == 1
    assert text.count("## Round 3 — Implementer (attempt 2)") == 1
    assert "interrupted" not in text
    assert "resumed" in text
    assert "retry" in text


def test_progress_writes_profiler_summary_with_perf(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    summary = ProfilerSummary(
        analysis="launch-bound",
        bottlenecks="attention kernel 40%",
        suggestions="swap to flashinfer",
        perf_metric=8.2,
        perf_unit="req/s",
    )
    issue_board.append_profiler_summary(progress, 2, summary)
    text = progress.read_text()
    assert "Round 2 — Profiler" in text
    assert "perf_metric**: 8.2 req/s" in text
    assert "flashinfer" in text


def test_progress_append_implementer_and_judge(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    progress = tmp_path / "progress.md"
    issue_board.append_implementer(
        progress,
        3,
        1,
        ImplementerResponse(summary="added cuda graph", expected_behavior="replay works"),
    )
    issue_board.append_judge(
        progress,
        3,
        1,
        JudgeResponse(analysis="good", feedback="", verdict=Verdict.PASS),
    )
    text = progress.read_text()
    assert "Round 3 — Implementer (attempt 1)" in text
    assert "Round 3 — Judge (attempt 1)" in text
    assert "verdict**: pass" in text


def test_directory_memory_layout_splits_rounds_and_bounds_reads(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    roadmap, progress = issue_board.resolve_paths(tmp_path, "directories")
    issue_board.ensure_roadmap_file(roadmap)
    for round_number in range(1, 16):
        issue_board.append_pre_round_decision(
            progress,
            round_number,
            PreRoundDecision(
                need_profile=False,
                profile_focus="",
                reasoning=f"decision-{round_number}",
            ),
        )

    assert (roadmap / "index.md").exists()
    assert (progress / "round-0001.md").exists()
    assert (progress / "round-0015.md").exists()
    recent = issue_board.read_progress(progress)
    assert "## Round 11 —" not in recent
    assert "## Round 12 —" in recent
    assert "## Round 15 —" in recent


def test_framework_accuracy_gate_runs_manifest_command_and_records_pass(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _run_framework_accuracy_gate,
    )

    ctx = MagicMock()
    ctx.trusted_input_changes.return_value = []
    ctx.judge_accuracy_command = "trusted-check --profile hard"
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=0, output="PASS")
    progress = tmp_path / "progress.md"

    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=2,
        retry=1,
        progress_path=progress,
    )

    assert feedback is None
    ctx.judge_backend.execute.assert_called_once_with("trusted-check --profile hard")
    text = progress.read_text()
    assert "Framework accuracy gate" in text
    assert "verdict**: pass" in text
    assert "PASS" in text
    ctx.snapshot_workspace.assert_called_once_with("round-2-retry-1-framework-accuracy")


def test_framework_local_validation_executes_and_reuses_exact_inputs(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "server.py").write_text("READY = True\n")
    (workspace / "test_server.py").write_text("def test_ready(): assert True\n")
    progress = workspace / "progress"
    recipe = ValidationRecipe(
        name="focused-tests",
        command="uv run pytest -q test_server.py",
        input_paths=["server.py", "test_server.py"],
        timeout_seconds=120,
        purpose="Exercise the focused local server contract.",
    )
    recipe_artifact = workspace / "validation-recipes.json"
    recipe_artifact.write_text(
        json.dumps({"version": 1, "recipes": [recipe.model_dump(mode="json")]})
    )
    ctx = MagicMock()
    ctx.workspace = workspace
    ctx.git.current_sha.return_value = "a" * 40
    ctx.git.pending_changes.return_value = []
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=0, output="1 passed")

    feedback = _run_framework_validation_gate(
        ctx,
        recipe_artifact=recipe_artifact.name,
        round_number=1,
        retry=1,
        progress_path=progress,
    )

    assert feedback is None
    ctx.judge_backend.execute.assert_called_once_with(recipe.command, timeout=120)
    first = progress / "validation" / "round-0001-attempt-01.json"
    assert '"reused": false' in first.read_text()
    assert "Framework local validation" in (progress / "round-0001.md").read_text()

    ctx.judge_backend.execute.reset_mock()
    feedback = _run_framework_validation_gate(
        ctx,
        recipe_artifact=recipe_artifact.name,
        round_number=2,
        retry=1,
        progress_path=progress,
    )

    assert feedback is None
    ctx.judge_backend.execute.assert_not_called()
    second = progress / "validation" / "round-0002-attempt-01.json"
    assert '"reused": true' in second.read_text()


def test_framework_local_validation_fails_and_restores_mutation(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "server.py").write_text("READY = True\n")
    progress = workspace / "progress"
    recipe = ValidationRecipe(
        name="focused-tests",
        command="uv run pytest -q",
        input_paths=["server.py"],
        purpose="Exercise the focused local server contract.",
    )
    recipe_artifact = workspace / "validation-recipes.json"
    recipe_artifact.write_text(
        json.dumps({"version": 1, "recipes": [recipe.model_dump(mode="json")]})
    )
    ctx = MagicMock()
    ctx.workspace = workspace
    ctx.git.current_sha.return_value = "b" * 40
    ctx.git.pending_changes.return_value = ["server.py"]
    ctx.git.checkout_tree.return_value = True
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=0, output="1 passed")

    feedback = _run_framework_validation_gate(
        ctx,
        recipe_artifact=recipe_artifact.name,
        round_number=3,
        retry=1,
        progress_path=progress,
    )

    assert "mutated the workspace" in feedback  # pyright: ignore[reportOperatorIssue]  # tracked: #297
    ctx.git.checkout_tree.assert_called_once_with("b" * 40, clean=True)
    artifact = progress / "validation" / "round-0003-attempt-01.json"
    assert '"passed": false' in artifact.read_text()


def test_loop_runs_local_validation_only_after_judge_pass(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        implementer_outcomes=[HypothesisOutcome.NOMINATED],
        implementer_validation_artifacts=["validation-recipes.json"],
        judge_verdicts=["pass"],
    )

    with patch(
        "vibesys.loops.agent.loop._run_framework_validation_gate",
        return_value=None,
    ) as validation_gate:
        _invoke_orchestrate(
            tmp_path,
            ref_file,
            runner,
            max_rounds=1,
            judge_every=1,
            official_eval_every=10,
        )

    validation_gate.assert_called_once()
    assert validation_gate.call_args.kwargs["recipe_artifact"] == "validation-recipes.json"
    assert runner.counters["judge"] == 1


def test_framework_accuracy_gate_rejects_checker_failure(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _run_framework_accuracy_gate,
    )

    ctx = MagicMock()
    ctx.trusted_input_changes.return_value = []
    ctx.judge_accuracy_command = "trusted-check"
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=1, output="bad history")

    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=1,
        retry=2,
        progress_path=tmp_path / "progress.md",
    )

    assert "Framework accuracy gate failed" in feedback  # pyright: ignore[reportOperatorIssue]  # tracked: #297
    assert "bad history" in feedback  # pyright: ignore[reportOperatorIssue]  # tracked: #297


def test_framework_accuracy_gate_uses_manifest_timeout(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _run_framework_accuracy_gate,
    )

    ctx = MagicMock()
    ctx.trusted_input_changes.return_value = []
    ctx.judge_accuracy_command = "trusted-check"
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=0, output="PASS")
    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=1,
        retry=1,
        progress_path=tmp_path / "progress.md",
        timeout_seconds=300,
    )

    assert feedback is None
    ctx.judge_backend.execute.assert_called_once_with("trusted-check", timeout=300)


def test_framework_accuracy_gate_passes_candidate_revision_to_environment(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _run_framework_accuracy_gate,
    )

    ctx = MagicMock()
    ctx.trusted_input_changes.return_value = []
    ctx.judge_accuracy_command = "trusted-check"
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=0, output="PASS")

    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=1,
        retry=1,
        progress_path=tmp_path / "progress.md",
        candidate_revision="abc123",
    )

    assert feedback is None
    ctx.judge_backend.execute.assert_called_once_with(
        "env VIBESYS_CANDIDATE_REVISION=abc123 trusted-check"
    )


def test_framework_accuracy_gate_can_release_final_deployment(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _run_framework_accuracy_gate,
    )

    ctx = MagicMock()
    ctx.trusted_input_changes.return_value = []
    ctx.judge_accuracy_command = "trusted-check"
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=0, output="PASS")
    ctx.run_environment_view.deployment_release_env_var = "VIBESYS_RELEASE_MODAL_DEPLOYMENT"

    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=1,
        retry=1,
        progress_path=tmp_path / "progress.md",
        candidate_revision="abc123",
        release_deployment_after=True,
    )

    assert feedback is None
    ctx.judge_backend.execute.assert_called_once_with(
        "env VIBESYS_CANDIDATE_REVISION=abc123 VIBESYS_RELEASE_MODAL_DEPLOYMENT=1 trusted-check"
    )


def test_framework_accuracy_gate_rejects_evaluator_changes_without_execution(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _run_framework_accuracy_gate,
    )

    ctx = MagicMock()
    ctx.trusted_input_changes.return_value = ["_input_libs/checker.go"]
    ctx.judge_accuracy_command = "trusted-check"

    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=1,
        retry=1,
        progress_path=tmp_path / "progress.md",
    )

    assert "Evaluator-owned files were modified" in feedback  # pyright: ignore[reportOperatorIssue]  # tracked: #297
    ctx.judge_backend.execute.assert_not_called()


def test_framework_accuracy_gate_rejects_changes_during_execution(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _run_framework_accuracy_gate,
    )

    ctx = MagicMock()
    ctx.trusted_input_changes.side_effect = [[], ["_input_libs/checker.go"]]
    ctx.judge_accuracy_command = "trusted-check"
    ctx.judge_backend.execute.return_value = SimpleNamespace(exit_code=0, output="PASS")

    feedback = _run_framework_accuracy_gate(
        ctx,
        round_number=1,
        retry=1,
        progress_path=tmp_path / "progress.md",
    )

    assert "changed during accuracy execution" in feedback  # pyright: ignore[reportOperatorIssue]  # tracked: #297


def test_framework_gates_reuse_accuracy_pass_after_later_gate_failure(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent.loop import _run_framework_gates  # noqa: PLC0415  # tracked: #288

    ctx = MagicMock()
    ctx.agent_runner.backend_name = "deepagents"
    ctx.judge_accuracy_command = "trusted-check"
    progress = tmp_path / "progress.md"

    with (
        patch(
            "vibesys.loops.agent.loop._run_framework_accuracy_gate",
            return_value=None,
        ) as accuracy_gate,
        patch(
            "vibesys.loops.agent.loop._run_framework_benchmark",
            side_effect=[("benchmark failed", None), (None, 42.0)],
        ),
    ):
        first = _run_framework_gates(
            ctx,
            benchmark_result=MagicMock(),
            round_number=3,
            retry=1,
            progress_path=progress,
        )
        second = _run_framework_gates(
            ctx,
            benchmark_result=MagicMock(),
            round_number=3,
            retry=2,
            progress_path=progress,
            reuse_accuracy_pass=True,
        )

    assert first == ("benchmark failed", None, True)
    assert second == (None, 42.0, True)
    accuracy_gate.assert_called_once()
    assert "Reused the prior framework-owned PASS" in progress.read_text()


def test_framework_benchmark_extracts_declared_metric(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _FRAMEWORK_BENCHMARK_END_MARKER,
        _FRAMEWORK_BENCHMARK_MARKER,
        _run_framework_benchmark,
    )

    ctx = MagicMock()
    ctx.judge_benchmark_command = "trusted-benchmark --repetitions 3"
    ctx.trusted_input_changes.side_effect = [[], []]
    ctx.judge_backend.execute.return_value = SimpleNamespace(
        exit_code=0,
        output=(
            f"benchmark diagnostics\n{_FRAMEWORK_BENCHMARK_MARKER}\n"
            f'[{{"total_ops_per_sec": 42.5}}]\n{_FRAMEWORK_BENCHMARK_END_MARKER}\n'
            "[stderr] benchmark diagnostics emitted after stdout"
        ),
    )

    feedback, metric = _run_framework_benchmark(
        ctx,
        result_spec=BenchmarkResult(
            json_argument="--output-json",
            metric="total_ops_per_sec",
        ),
        round_number=3,
        retry=1,
        progress_path=tmp_path / "progress.md",
        timeout_seconds=300,
    )

    assert feedback is None
    assert metric == 42.5
    executed = ctx.judge_backend.execute.call_args.args[0]
    assert ctx.judge_backend.execute.call_args.kwargs == {"timeout": 300}
    assert "trusted-benchmark --repetitions 3 --output-json" in executed
    assert "cat /tmp/vibesys-framework-benchmark-3-1.json" in executed
    assert _FRAMEWORK_BENCHMARK_END_MARKER in executed
    assert "total_ops_per_sec**: 42.5" in (tmp_path / "progress.md").read_text()


def test_framework_benchmark_prefers_top_level_metric_over_trial_diagnostics(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _FRAMEWORK_BENCHMARK_END_MARKER,
        _FRAMEWORK_BENCHMARK_MARKER,
        _run_framework_benchmark,
    )

    ctx = MagicMock()
    ctx.judge_benchmark_command = "trusted-benchmark"
    ctx.trusted_input_changes.side_effect = [[], []]
    ctx.judge_backend.execute.return_value = SimpleNamespace(
        exit_code=0,
        output=(
            f"{_FRAMEWORK_BENCHMARK_MARKER}\n"
            '{"primary_value": 42.5, "trials": [{"primary_value": 41.0}, '
            '{"primary_value": 44.0}]}\n'
            f"{_FRAMEWORK_BENCHMARK_END_MARKER}"
        ),
    )

    feedback, metric = _run_framework_benchmark(
        ctx,
        result_spec=BenchmarkResult(json_argument="--json", metric="primary_value"),
        round_number=1,
        retry=1,
        progress_path=tmp_path / "progress.md",
    )

    assert feedback is None
    assert metric == 42.5


def test_framework_benchmark_rejects_ambiguous_metric(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.input_manifest import BenchmarkResult  # noqa: PLC0415  # tracked: #288
    from vibesys.loops.agent.loop import (  # noqa: PLC0415  # tracked: #288
        _FRAMEWORK_BENCHMARK_END_MARKER,
        _FRAMEWORK_BENCHMARK_MARKER,
        _run_framework_benchmark,
    )

    ctx = MagicMock()
    ctx.judge_benchmark_command = "trusted-benchmark"
    ctx.trusted_input_changes.side_effect = [[], []]
    ctx.judge_backend.execute.return_value = SimpleNamespace(
        exit_code=0,
        output=(
            f'{_FRAMEWORK_BENCHMARK_MARKER}\n[{{"ops": 1}}, {{"ops": 2}}]\n'
            f"{_FRAMEWORK_BENCHMARK_END_MARKER}"
        ),
    )

    feedback, metric = _run_framework_benchmark(
        ctx,
        result_spec=BenchmarkResult(json_argument="--json", metric="ops"),
        round_number=1,
        retry=1,
        progress_path=tmp_path / "progress.md",
    )

    assert metric is None
    assert "expected exactly one 'ops' field" in feedback  # pyright: ignore[reportOperatorIssue]  # tracked: #297


# ---------------------------------------------------------------------------
# Loop happy paths
# ---------------------------------------------------------------------------


def test_loop_round_one_no_profile_runs_one_round(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Round 1 skips pre-round-decision (no existing code), proposes one task,
    implementer+judge both pass. With max_rounds=1 the loop stops there."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build FastAPI server",
                pass_criteria="/health returns 200",  # noqa: S106  # tracked: #288
                reasoning="cold start",
            ),
        ],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert result is True
    # No pre-round decision on round 1 (no existing code).
    assert runner.counters["orch_plan"] == 1
    assert runner.counters["impl"] == 1
    assert runner.counters["judge"] == 1


def test_agent_roles_reference_framework_owned_effective_objective(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    effective = (
        "Optimize the service.\n\n## Operator constraints\n\n- simultaneous exact H100/BF16\n"
    )
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Optimize the BF16 path",
                pass_criteria="BF16 remains active",  # noqa: S106  # tracked: #288
                reasoning="respect the hard precision constraint",
            )
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        objective=effective,
        max_rounds=1,
    )

    objective_path = next((tmp_path / "exp_env").glob("*/logs/effective-objective.md"))
    assert objective_path.read_text() == effective
    for call in runner.invoke.call_args_list:
        if call.kwargs.get("kind") not in {"orchestrator", "implementer", "judge"}:
            continue
        prompt = call.kwargs["system_prompt"]
        assert str(objective_path) in prompt
        assert "simultaneous exact H100/BF16" not in prompt


def test_implementer_skill_updates_survive_a_renewed_continuation_prompt(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    skill = tmp_path / "skills" / "portable"
    reference = skill / "references" / "transport.md"
    reference.parent.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        "---\nname: portable\ndescription: Portable transport guidance.\n---\n"
    )
    reference.write_text("transport guidance\n")
    selection = SkillResourceSelection(
        skill="portable",
        resource_paths=["references/transport.md"],
        purpose="Replace the request-local transport boundary.",
    )
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="transport-boundary",
                task="Replace request-local fanout.",
                pass_criteria="The direct path activates.",  # noqa: S106  # tracked: #288
                reasoning="The residual is host-side.",
            )
        ],
        implementer_outcomes=[HypothesisOutcome.CONTINUE, HypothesisOutcome.NOMINATED],
        implementer_skill_updates=[[selection], []],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        judge_every=10,
        skills_dirs=[str(skill)],
    )

    calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("session_key") == "hypothesis:transport-boundary"
    ]
    assert len(calls) == 2
    assert "portable/references/transport.md" not in calls[0].kwargs["system_prompt"]
    assert "portable/SKILL.md" in calls[1].kwargs["system_prompt"]
    assert "portable/references/transport.md" in calls[1].kwargs["system_prompt"]

    plan_path = calls[1].kwargs["workspace"] / "progress-artifacts" / "plans" / "round-0002.json"
    persisted = OrchestratorPlan.model_validate_json(plan_path.read_text())
    assert persisted.recommended_skills == [selection]
    assert runner.counters["prof"] == 0


def test_loop_judge_retry_then_pass(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Judge fails once, implementer retries, judge passes. Loop bounded by max_rounds=1."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build server",
                pass_criteria="tests pass",  # noqa: S106  # tracked: #288
                reasoning="cold start",
            ),
        ],
        judge_verdicts=["fail", "pass"],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1, max_retries_per_round=3)
    assert result is True
    assert runner.counters["impl"] == 2
    assert runner.counters["judge"] == 2
    implementer_calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is ImplementerResponse
    ]
    assert "Same-round retry boundary" not in implementer_calls[0].kwargs["system_prompt"]
    retry_prompt = implementer_calls[1].kwargs["system_prompt"]
    assert "Same-round retry boundary" in retry_prompt
    assert "round-0001-attempt-01-implementer.json" in retry_prompt
    assert "remains consumed" in retry_prompt


def test_loop_defers_judge_until_cadence_and_always_reviews_final_round(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="graph-decode",
                hypothesis="graph replay removes launch overhead",
                task=f"continue graph work {round_number}",
                pass_criteria="activation evidence is real",  # noqa: S106  # tracked: #288
                reasoning="continue one causal experiment",
            )
            for round_number in range(1, 4)
        ],
        implementer_outcomes=[HypothesisOutcome.CONTINUE] * 3,
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=3,
        judge_every=2,
    )

    assert result is True
    assert runner.counters["impl"] == 3
    assert runner.counters["judge"] == 2  # cadence round 2 + mandatory final round 3
    rounds_files = list((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_files[0].read_text())
    assert [round_data["reviewed"] for round_data in rounds] == [False, True, True]
    progress_files = list((tmp_path / "exp_env").glob("*/workspace/progress.md"))
    assert "Independent review deferred" in progress_files[0].read_text()


def test_nominated_candidate_gets_early_review(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        implementer_outcomes=[HypothesisOutcome.NOMINATED],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        judge_every=10,
    )

    assert runner.counters["judge"] >= 1


def test_official_gates_run_on_candidate_cadence_and_final_round(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        implementer_outcomes=[HypothesisOutcome.NOMINATED] * 4,
        implementer_perf_metrics=[10.0, 20.0, 30.0, 40.0],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=4,
        judge_every=10,
        official_eval_every=3,
        _accuracy_gate_results=[None, None],
    )

    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [record["official_evaluation"] for record in rounds] == [
        False,
        False,
        True,
        True,
    ]
    assert [record["official_evaluation_reason"] for record in rounds] == [
        None,
        None,
        "cadence",
        "final_round",
    ]
    assert [record["perf_metric"] for record in rounds] == [None, None, 30.0, 40.0]


def test_orchestrator_can_request_official_evaluation_before_cadence(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="checkpoint-now",
                task="finish a likely new best",
                pass_criteria="targeted comparison passes",  # noqa: S106  # tracked: #288
                request_official_evaluation=True,
                reasoning="the next branch needs a verified parent",
            ),
            OrchestratorPlan(
                hypothesis_id="continue-after-checkpoint",
                task="make another improvement",
                pass_criteria="targeted comparison passes",  # noqa: S106  # tracked: #288
                reasoning="continue from verified evidence",
            ),
        ],
        implementer_outcomes=[HypothesisOutcome.NOMINATED] * 2,
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        official_eval_every=10,
        _accuracy_gate_results=[None, None],
    )

    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [record["official_evaluation_reason"] for record in rounds] == [
        "orchestrator_request",
        "final_round",
    ]


def test_supported_hypothesis_is_reviewed_without_global_gates_and_closes(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="diagnostic-one",
                hypothesis="the diagnostic identifies the bottleneck",
                task="collect the scoped evidence",
                pass_criteria="retain the diagnostic artifact",  # noqa: S106  # tracked: #288
                reasoning="finish one bounded diagnostic",
            ),
            OrchestratorPlan(
                hypothesis_id="mechanism-two",
                hypothesis="a new mechanism can use that evidence",
                task="start the next experiment",
                pass_criteria="retain causal evidence",  # noqa: S106  # tracked: #288
                reasoning="the prior diagnostic is complete",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.SUPPORTED,
            HypothesisOutcome.SUPPORTED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        judge_every=10,
        _accuracy_gate_results=[None],
    )

    assert runner.counters["orch_plan"] == 2
    assert runner.counters["impl"] == 2
    assert runner.counters["judge"] == 2
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_outcome"] for round_data in rounds] == [
        "proven",
        "proven",
    ]
    assert [round_data["official_evaluation"] for round_data in rounds] == [False, True]
    assert rounds[-1]["official_evaluation_reason"] == "final_round"


def test_cadence_pass_keeps_a_continuing_hypothesis_active(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="multi-round-experiment",
                hypothesis="one causal claim needs multiple rounds",
                task="run the experiment",
                pass_criteria="retain auditable evidence",  # noqa: S106  # tracked: #288
                reasoning="start one bounded experiment",
            )
        ],
        implementer_outcomes=[
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.NOMINATED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=3,
        judge_every=2,
    )

    # Round 2's cadence review validates the provisional implementation but
    # must not hand design ownership back to the outer agent. The same inner
    # agent finishes the hypothesis and nominates it in round 3.
    assert runner.counters["orch_plan"] == 1
    assert runner.counters["impl"] == 3
    assert runner.counters["judge"] == 2
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_outcome"] for round_data in rounds] == [
        "continue",
        "continue",
        "proven",
    ]


def test_implementation_failure_with_repair_keeps_hypothesis_active(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="repairable-mechanism",
                hypothesis="the mechanism helps after its runtime defect is repaired",
                task="implement and test the mechanism",
                pass_criteria="retain causal evidence",  # noqa: S106  # tracked: #288
                reasoning="one persistent hypothesis",
            )
        ],
        implementer_outcomes=[
            HypothesisOutcome.IMPLEMENTATION_FAILED,
            HypothesisOutcome.NOMINATED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        judge_every=10,
    )

    assert runner.counters["orch_plan"] == 1
    assert runner.counters["impl"] == 2
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "repairable-mechanism",
        "repairable-mechanism",
    ]
    assert [round_data["hypothesis_outcome"] for round_data in rounds] == [
        "implementation_failed",
        "proven",
    ]


def test_repeated_implementation_failures_return_control_to_designer(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="repair-lease",
                hypothesis="the mechanism helps after target-only repairs",
                task="implement and repair the mechanism",
                pass_criteria="retain causal evidence",  # noqa: S106  # tracked: #288
                reasoning="start one persistent implementation lease",
            ),
            OrchestratorPlan(
                hypothesis_id="designer-review",
                hypothesis="compare the stalled repair against alternatives",
                task="choose the next bounded experiment",
                pass_criteria="retain a reviewed direction",  # noqa: S106  # tracked: #288
                reasoning="the repair lease expired",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.IMPLEMENTATION_FAILED,
            HypothesisOutcome.IMPLEMENTATION_FAILED,
            HypothesisOutcome.IMPLEMENTATION_FAILED,
            HypothesisOutcome.NOMINATED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=4,
        judge_every=10,
    )

    assert runner.counters["orch_plan"] == 2
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "repair-lease",
        "repair-lease",
        "repair-lease",
        "designer-review",
    ]


def test_repeated_continue_outcomes_return_control_to_designer(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="self-renewing-lease",
                hypothesis="the mechanism needs several implementation steps",
                task="implement and test the mechanism",
                pass_criteria="retain causal evidence",  # noqa: S106  # tracked: #288
                reasoning="start one bounded implementation lease",
            ),
            OrchestratorPlan(
                hypothesis_id="review-after-continue",
                hypothesis="compare the unfinished mechanism against alternatives",
                task="choose the next bounded experiment",
                pass_criteria="retain a reviewed direction",  # noqa: S106  # tracked: #288
                reasoning="the continuation lease expired",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.NOMINATED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=4,
        judge_every=10,
    )

    assert runner.counters["orch_plan"] == 2
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "self-renewing-lease",
        "self-renewing-lease",
        "self-renewing-lease",
        "review-after-continue",
    ]


def test_repeated_rejected_reviews_return_control_to_designer(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="rejected-review-lease",
                hypothesis="the candidate needs bounded evidence repair",
                task="repair and present the evidence",
                pass_criteria="retain causal evidence",  # noqa: S106  # tracked: #288
                reasoning="start one bounded review-repair lease",
            ),
            OrchestratorPlan(
                hypothesis_id="review-after-rejections",
                hypothesis="compare the repeatedly rejected work against alternatives",
                task="choose the next bounded experiment",
                pass_criteria="retain a reviewed direction",  # noqa: S106  # tracked: #288
                reasoning="the review-repair lease expired",
            ),
        ],
        implementer_outcomes=[HypothesisOutcome.NOMINATED] * 4,
        judge_verdicts=["fail", "fail", "fail", "pass"],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=4,
        max_retries_per_round=1,
        judge_every=1,
    )

    assert runner.counters["orch_plan"] == 2
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "rejected-review-lease",
        "rejected-review-lease",
        "rejected-review-lease",
        "review-after-rejections",
    ]


def test_resolvable_inconclusive_result_keeps_hypothesis_active(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="variance-boundary",
                hypothesis="one repeat resolves the causal classification",
                task="measure and repeat only if ambiguous",
                pass_criteria="retain a variance-aware classification",  # noqa: S106  # tracked: #288
                reasoning="one persistent hypothesis",
            )
        ],
        implementer_outcomes=[
            HypothesisOutcome.INCONCLUSIVE,
            HypothesisOutcome.NOMINATED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        judge_every=10,
    )

    assert runner.counters["orch_plan"] == 1
    assert runner.counters["impl"] == 2
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "variance-boundary",
        "variance-boundary",
    ]
    assert [round_data["hypothesis_outcome"] for round_data in rounds] == [
        "inconclusive",
        "proven",
    ]


def test_cadence_review_is_not_duplicated_for_provisional_retry(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="stable-hypothesis",
                hypothesis="same causal claim",
                task="continue the experiment",
                pass_criteria="retain causal evidence",  # noqa: S106  # tracked: #288
                reasoning="one hypothesis across rounds",
            ),
            OrchestratorPlan(
                hypothesis_id="replacement-hypothesis",
                hypothesis="next causal claim",
                task="finish the replacement experiment",
                pass_criteria="retain causal evidence",  # noqa: S106  # tracked: #288
                reasoning="the prior claim passed review",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.NOMINATED,
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.NOMINATED,
        ],
        judge_verdicts=["fail", "pass"],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=4,
        max_retries_per_round=2,
        judge_every=3,
    )

    # Round 3 receives one cadence review.  Its provisional retry carries the
    # feedback forward without paying for the same independent audit again.
    # The final-round nomination is still reviewed immediately.
    assert runner.counters["impl"] == 5
    assert runner.counters["judge"] == 2


def test_unreviewed_terminal_outcome_returns_control_to_designer(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="falsified-path",
                hypothesis="first claim",
                task="test first claim",
                pass_criteria="collect evidence",  # noqa: S106  # tracked: #288
                reasoning="first experiment",
            ),
            OrchestratorPlan(
                hypothesis_id="replacement-path",
                hypothesis="second claim",
                task="test second claim",
                pass_criteria="collect evidence",  # noqa: S106  # tracked: #288
                reasoning="replacement experiment",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.DISPROVEN,
            HypothesisOutcome.NOMINATED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        judge_every=10,
    )

    assert runner.counters["orch_plan"] == 2
    assert runner.counters["judge"] == 1  # only the replacement, on the final round
    rounds_files = list((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_files[0].read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "falsified-path",
        "replacement-path",
    ]
    assert [round_data["hypothesis_outcome"] for round_data in rounds] == [
        "disproven",
        "proven",
    ]
    plan_calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is OrchestratorPlan
    ]
    assert "latest progress entry contains a regression or terminal-workspace notice" in (
        plan_calls[1].kwargs["system_prompt"].replace("\n", " ").lower()
    )


def test_reviewed_disproof_skips_framework_gates(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        implementer_outcomes=[HypothesisOutcome.DISPROVEN],
        judge_verdicts=["pass"],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=1,
        judge_every=1,
        _accuracy_gate_results=[None],
    )

    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert rounds[0]["passed"] is True
    assert rounds[0]["reviewed"] is True
    assert rounds[0]["hypothesis_outcome"] == "disproven"
    assert rounds[0]["official_evaluation"] is True
    assert rounds[0]["official_evaluation_reason"] == "final_round"


def test_disproven_retry_after_failed_review_returns_control_to_designer(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="falsified-after-review",
                hypothesis="first claim",
                task="test first claim",
                pass_criteria="collect evidence",  # noqa: S106  # tracked: #288
                reasoning="first experiment",
            ),
            OrchestratorPlan(
                hypothesis_id="replacement-after-review",
                hypothesis="second claim",
                task="test second claim",
                pass_criteria="collect evidence",  # noqa: S106  # tracked: #288
                reasoning="replacement experiment",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.NOMINATED,
            HypothesisOutcome.DISPROVEN,
            HypothesisOutcome.NOMINATED,
        ],
        judge_verdicts=["fail", "pass", "pass"],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        max_retries_per_round=2,
        judge_every=10,
    )

    assert runner.counters["orch_plan"] == 2
    assert runner.counters["impl"] == 3
    assert runner.counters["judge"] == 3
    rounds_file = next((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_file.read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "falsified-after-review",
        "replacement-after-review",
    ]
    assert [round_data["reviewed"] for round_data in rounds] == [True, True]
    assert [round_data["hypothesis_outcome"] for round_data in rounds] == [
        "disproven",
        "proven",
    ]


def test_role_session_policy_is_explicit_and_hypothesis_scoped(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="stable-hypothesis",
                hypothesis="same claim",
                task="continue",
                pass_criteria="review",  # noqa: S106  # tracked: #288
                reasoning="same experiment",
            ),
            OrchestratorPlan(
                hypothesis_id="stable-hypothesis",
                hypothesis="same claim",
                task="finish",
                pass_criteria="review",  # noqa: S106  # tracked: #288
                reasoning="same experiment",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.NOMINATED,
        ],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        judge_every=10,
    )

    calls = runner.invoke.call_args_list
    plan_calls = [call for call in calls if call.kwargs.get("response_cls") is OrchestratorPlan]
    implementer_calls = [
        call for call in calls if call.kwargs.get("response_cls") is ImplementerResponse
    ]
    judge_calls = [call for call in calls if call.kwargs.get("response_cls") is JudgeResponse]
    # The outer designer hands off one causal claim and is not re-invoked
    # while the implementer reports that same hypothesis as continuing.
    assert len(plan_calls) == 1
    assert len(implementer_calls) == 2
    assert all(call.kwargs["reuse_session"] is False for call in plan_calls)
    assert all(call.kwargs["reuse_session"] is True for call in implementer_calls)
    assert {call.kwargs["session_key"] for call in implementer_calls} == {
        "hypothesis:stable-hypothesis"
    }
    assert (
        "Required continuation from the previous round"
        not in implementer_calls[0].kwargs["system_prompt"]
    )
    assert "continue experiment" in implementer_calls[1].kwargs["system_prompt"]
    assert "do not merely restate prior work" in implementer_calls[1].kwargs["user_prompt"]
    assert all(call.kwargs["reuse_session"] is False for call in judge_calls)

    rounds_files = list((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_files[0].read_text())
    assert [round_data["hypothesis_id"] for round_data in rounds] == [
        "stable-hypothesis",
        "stable-hypothesis",
    ]
    assert [round_data["hypothesis_outcome"] for round_data in rounds] == [
        "continue",
        "proven",
    ]


def test_rejected_terminal_submission_cannot_schedule_framework_gate_as_next_step(  # noqa: ANN201  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
):
    forbidden_step = "Run the framework-owned accuracy evaluation."
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="gate-ownership",
                hypothesis="candidate is ready",
                task="prepare candidate evidence",
                pass_criteria="review",  # noqa: S106  # tracked: #288
                reasoning="framework owns official gates",
            )
        ],
        implementer_outcomes=[
            HypothesisOutcome.NOMINATED,
            HypothesisOutcome.NOMINATED,
        ],
        implementer_next_steps=[forbidden_step, ""],
        judge_verdicts=["fail", "pass"],
    )

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        max_retries_per_round=1,
        judge_every=1,
    )

    implementer_calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is ImplementerResponse
    ]
    assert len(implementer_calls) == 2
    retry_prompt = implementer_calls[1].kwargs["system_prompt"]
    assert forbidden_step not in retry_prompt
    assert "Current review delta" in retry_prompt
    assert "needs work" in retry_prompt
    assert "do not duplicate framework-owned commands" in retry_prompt


def test_hypothesis_revert_is_applied_once_across_continuation_rounds(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="seed",
                task="establish parent",
                pass_criteria="review",  # noqa: S106  # tracked: #288
                reasoning="seed checkpoint",
            ),
            OrchestratorPlan(
                hypothesis_id="continued-repair",
                task="start from parent and continue",
                pass_criteria="review",  # noqa: S106  # tracked: #288
                revert_to_round=1,
                reasoning="discard a later branch once",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.NOMINATED,
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.NOMINATED,
        ],
    )

    with patch(
        "vibesys.run.git_tracker.GitTracker.checkout_tree", return_value=True
    ) as checkout_tree:
        _invoke_orchestrate(
            tmp_path,
            ref_file,
            runner,
            max_rounds=3,
            judge_every=10,
        )

    assert checkout_tree.call_count == 1
    checkout_tree.assert_called_once_with(
        ANY,
        clean=True,
        preserve_paths=("roadmap.md", "progress.md", "pareto-frontier.md"),
    )
    repair_calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is ImplementerResponse
        and call.kwargs.get("session_key") == "hypothesis:continued-repair"
    ]
    assert len(repair_calls) == 2
    assert all(
        "framework already materialized" in call.kwargs["system_prompt"].lower()
        for call in repair_calls
    )
    assert all(
        "do not re-run checkout" in call.kwargs["system_prompt"].lower()
        or "do not repeat restoration" in call.kwargs["system_prompt"].lower()
        for call in repair_calls
    )
    assert all(
        "reuse valid retained parent rows"
        in call.kwargs["system_prompt"].replace("\n", " ").lower()
        or "do not repeat restoration or parent measurement"
        in call.kwargs["system_prompt"].replace("\n", " ").lower()
        for call in repair_calls
    )
    judge_calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is JudgeResponse
        and call.kwargs.get("round_label", "").startswith(("round-2-", "round-3-"))
    ]
    assert judge_calls
    assert all(
        "framework authoritatively materialized" in call.kwargs["system_prompt"].lower()
        for call in judge_calls
    )
    assert all(
        "do not demand candidate-supplied git proof"
        in call.kwargs["system_prompt"].replace("\n", " ").lower()
        for call in judge_calls
    )
    assert all(
        "duplicate measurement of a trustworthy retained parent row"
        in call.kwargs["system_prompt"].replace("\n", " ")
        for call in judge_calls
    )


def test_failed_hypothesis_revert_is_retried_and_not_claimed_as_applied(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                hypothesis_id="seed",
                task="establish parent",
                pass_criteria="review",  # noqa: S106  # tracked: #288
                reasoning="seed checkpoint",
            ),
            OrchestratorPlan(
                hypothesis_id="retry-rollback",
                task="start from parent",
                pass_criteria="review",  # noqa: S106  # tracked: #288
                revert_to_round=1,
                reasoning="restore parent",
            ),
        ],
        implementer_outcomes=[
            HypothesisOutcome.NOMINATED,
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.NOMINATED,
        ],
    )

    with patch(
        "vibesys.run.git_tracker.GitTracker.checkout_tree",
        side_effect=[False, True],
    ) as checkout_tree:
        _invoke_orchestrate(
            tmp_path,
            ref_file,
            runner,
            max_rounds=3,
            judge_every=10,
        )

    assert checkout_tree.call_count == 2
    retry_calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is ImplementerResponse
        and call.kwargs.get("session_key") == "hypothesis:retry-rollback"
    ]
    assert len(retry_calls) == 2
    assert "framework already materialized" not in retry_calls[0].kwargs["system_prompt"].lower()
    assert "framework already materialized" in retry_calls[1].kwargs["system_prompt"].lower()


def test_judge_audited_implementer_metrics_are_recorded(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(implementer_perf_metrics=[321.5])

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=1,
        judge_every=10,
    )

    rounds_files = list((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_files[0].read_text())
    assert rounds[0]["perf_metric"] == 321.5
    assert rounds[0]["perf_unit"] == "tok/s"
    assert rounds[0]["metrics"] == {
        "aggregate_throughput": 321.5,
        "p99_latency_ms": 87.0,
    }
    assert rounds[0]["evaluation_artifact"] == "benchmark/summary.json"
    assert rounds[0]["profile_skipped"] is False

    judge_call = next(
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is JudgeResponse
    )
    assert "321.5 tok/s" not in judge_call.kwargs["system_prompt"]
    assert "benchmark/summary.json" not in judge_call.kwargs["system_prompt"]
    evidence_files = list(
        judge_call.kwargs["workspace"].glob(
            "progress-artifacts/evidence/round-0001-attempt-01-implementer.json"
        )
    )
    assert len(evidence_files) == 1
    evidence = ImplementerResponse.model_validate_json(evidence_files[0].read_text())
    assert evidence.perf_metric == 321.5
    assert evidence.perf_unit == "tok/s"
    assert evidence.evaluation_artifact == "benchmark/summary.json"
    assert (
        evidence_files[0].relative_to(judge_call.kwargs["workspace"]).as_posix()
        in (judge_call.kwargs["system_prompt"])
    )


def test_loop_retries_when_framework_accuracy_gate_fails(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[OrchestratorPlan(task="Build", pass_criteria="tests", reasoning="start")],  # noqa: S106  # tracked: #288
        judge_verdicts=["pass", "pass"],
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=1,
        max_retries_per_round=2,
        _accuracy_gate_results=["checker rejected history", None],
    )

    assert result is True
    assert runner.counters["impl"] == 2
    assert runner.counters["judge"] == 2


def test_framework_gate_retry_preserves_judge_approved_metrics(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        plans=[OrchestratorPlan(task="Build", pass_criteria="tests", reasoning="start")],  # noqa: S106  # tracked: #288
        judge_verdicts=["pass", "pass"],
        implementer_perf_metrics=[321.5, None],
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=1,
        max_retries_per_round=2,
        _accuracy_gate_results=["wrapper failed", None],
    )

    assert result is True
    rounds_files = list((tmp_path / "exp_env").glob("*/logs/rounds.json"))
    rounds = __import__("json").loads(rounds_files[0].read_text())
    assert rounds[0]["perf_metric"] == 321.5
    assert rounds[0]["perf_unit"] == "tok/s"
    assert rounds[0]["metrics"] == {
        "aggregate_throughput": 321.5,
        "p99_latency_ms": 87.0,
    }
    assert rounds[0]["evaluation_artifact"] == "benchmark/summary.json"
    assert rounds[0]["profile_skipped"] is False

    implementer_calls = [
        call
        for call in runner.invoke.call_args_list
        if call.kwargs.get("response_cls") is ImplementerResponse
    ]
    assert len(implementer_calls) == 2
    retry_prompt = implementer_calls[1].kwargs["system_prompt"]
    assert "Framework gate revalidation" in retry_prompt
    assert "321.5 tok/s" in retry_prompt
    assert "behavior-affecting repair invalidates stale metrics" in retry_prompt
    assert "do not duplicate the canonical run" in retry_prompt


def test_loop_exhaustion_carries_to_next_round(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Review exhaustion returns to the same implementer, not the designer."""
    seen_plan_prompts: list[str] = []
    seen_implementer_prompts: list[str] = []
    original_runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build the whole server with every optimization",
                pass_criteria="impossibly strict",  # noqa: S106  # tracked: #288
                reasoning="ambitious",
            ),
            OrchestratorPlan(
                task="Just get /health working",
                pass_criteria="/health returns 200",  # noqa: S106  # tracked: #288
                reasoning="backed off after exhaustion",
            ),
        ],
        judge_verdicts=["fail", "fail", "pass"],
    )

    # Wrap invoke so we can capture the orchestrator plan prompts.
    real_invoke = original_runner.invoke.side_effect

    def spy_invoke(*, kind, response_cls, **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            seen_plan_prompts.append(kwargs.get("system_prompt", ""))
        if kind == "implementer" and response_cls is ImplementerResponse:
            seen_implementer_prompts.append(kwargs.get("system_prompt", ""))
        return real_invoke(kind=kind, response_cls=response_cls, **kwargs)

    original_runner.invoke.side_effect = spy_invoke

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        original_runner,
        max_rounds=2,
        max_retries_per_round=2,
    )
    assert result is True
    # 2 attempts on round 1 (both fail) + 1 attempt on round 2 (pass).
    assert original_runner.counters["impl"] == 3
    # The outer designer is hands-off. Round 2 reuses the active plan and the
    # persistent implementer receives the independent judge's last feedback.
    assert len(seen_plan_prompts) == 1
    assert "needs work" in seen_implementer_prompts[2]


def test_loop_orchestrator_requests_profile_before_plan(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """If PreRoundDecision.need_profile is True, profiler runs before the plan call."""
    call_order: list[str] = []
    profiler_prompts: list[str] = []
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="kernels", reasoning="need data"),
        ],
        plans=[
            # Round 1 cold-start plan (no pre-decision invoked on round 1).
            OrchestratorPlan(
                task="Build server",
                pass_criteria="ok",  # noqa: S106  # tracked: #288
                reasoning="start",
            ),
            # Round 2 plan — uses profiler summary.
            OrchestratorPlan(
                task="Optimize decode",
                pass_criteria="graph replay",  # noqa: S106  # tracked: #288
                reasoning="profile showed launch overhead",
            ),
        ],
        profiler_responses=[
            ProfilerSummary(
                analysis="launch-bound",
                bottlenecks="host-side sync",
                suggestions="cuda graph",
                perf_metric=5.0,
                perf_unit="req/s",
            ),
        ],
    )

    real_invoke = runner.invoke.side_effect

    def spy_invoke(*, kind, response_cls, **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            call_order.append("plan")
        elif kind == "profiler":
            call_order.append("profiler")
            profiler_prompts.append(kwargs.get("system_prompt", ""))
        elif kind == "orchestrator" and response_cls is PreRoundDecision:
            call_order.append("pre")
        return real_invoke(kind=kind, response_cls=response_cls, **kwargs)

    runner.invoke.side_effect = spy_invoke

    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=2)
    assert result is True
    # Round 1 cold-start: no pre → just plan.
    # Round 2: pre → profiler → plan.
    assert call_order[:1] == ["plan"]
    assert "profiler" in call_order
    plan_idx = [i for i, c in enumerate(call_order) if c == "plan"]
    prof_idx = call_order.index("profiler")
    # Profiler must come BEFORE the round-2 plan call.
    assert prof_idx < plan_idx[1]
    assert "Recent campaign context" in profiler_prompts[0]
    assert "The durable progress artifact is `progress.md`" in profiler_prompts[0]
    assert "Round 1" not in profiler_prompts[0]
    assert "Do not launch a duplicate expensive evaluation" in profiler_prompts[0]


def test_loop_skips_profiler_when_pre_round_decision_says_no(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=False, profile_focus="", reasoning="benchmark is enough"),
        ],
        plans=[
            OrchestratorPlan(task="Build server", pass_criteria="ok", reasoning="start"),  # noqa: S106  # tracked: #288
            OrchestratorPlan(task="Use benchmark evidence", pass_criteria="ok", reasoning="skip"),  # noqa: S106  # tracked: #288
        ],
    )

    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=2)

    assert result is True
    assert runner.counters["orch_pre"] == 1
    assert runner.counters["prof"] == 0


def test_loop_skips_profiler_when_profiler_kind_is_none(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="kernels", reasoning="would help"),
        ],
        plans=[
            OrchestratorPlan(task="Build server", pass_criteria="ok", reasoning="start"),  # noqa: S106  # tracked: #288
            OrchestratorPlan(
                task="Use benchmark evidence",
                pass_criteria="ok",  # noqa: S106  # tracked: #288
                reasoning="disabled",  # noqa: RUF100, S106  # tracked: #288
            ),
        ],
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        profiler_kind=ProfilerKind.NONE,
    )

    assert result is True
    assert runner.counters["orch_pre"] == 1
    assert runner.counters["prof"] == 0


def test_loop_generic_auto_profiler_resolves_to_macos_cpu(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="kernels", reasoning="would help"),
        ],
        plans=[
            OrchestratorPlan(task="Build queue", pass_criteria="ok", reasoning="start"),  # noqa: S106  # tracked: #288
            OrchestratorPlan(
                task="Use benchmark evidence",
                pass_criteria="ok",  # noqa: S106  # tracked: #288
                reasoning="generic",  # noqa: RUF100, S106  # tracked: #288
            ),
        ],
    )

    with (
        patch("vibesys.profilers.platform.system", return_value="Darwin"),
        patch(
            "vibesys.context.preflight_profiler_kind",
            lambda kind: ProfilerPreflightResult(kind, True),  # noqa: FBT003  # tracked: #288
        ),
    ):
        result = _invoke_orchestrate(
            tmp_path,
            ref_file,
            runner,
            max_rounds=2,
            domain=DomainName.GENERIC,
        )

    assert result is True
    assert runner.counters["orch_pre"] == 1
    assert runner.counters["prof"] == 1


def test_loop_runs_full_max_rounds_budget(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """With the ``done`` field removed, the loop always exhausts max_rounds.
    A single-round budget yields one implementer + judge call, no more."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build server",
                pass_criteria="ok",  # noqa: S106  # tracked: #288
                reasoning="round 1",
            )
        ],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert result is True
    assert runner.counters["impl"] == 1
    assert runner.counters["judge"] == 1


def test_loop_max_rounds_terminates(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """Loop exits after max_rounds and reports success (the loop always runs
    to budget; there is no early-stop signal)."""
    plans = [OrchestratorPlan(task=f"t{i}", pass_criteria="p", reasoning="r") for i in range(10)]  # noqa: S106  # tracked: #288
    runner = _make_orchestrate_runner(plans=plans)
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=3)
    assert result is True
    assert runner.counters["orch_plan"] == 3
    assert runner.counters["impl"] == 3


# ---------------------------------------------------------------------------
# CLI / OBJECTIVE.md discovery
# ---------------------------------------------------------------------------


def test_cli_loads_objective_md_from_ref_parent(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288
    from vibesys.main import _load_objective  # noqa: PLC0415  # tracked: #288

    bundle = tmp_path / "modelA"
    bundle.mkdir()
    (bundle / "OBJECTIVE.md").write_text("Maximize throughput (tok/s). Prefer CUDA graphs.\n")
    (bundle / "vibesys.input.toml").write_text(
        "version = 1\n\n"
        "[agent]\ndomain = 'llm-serving'\n\n"
        "[accuracy]\ncommand = ['uv', 'run', 'python', 'accuracy_checker/checker.py']\n\n"
        "[benchmark]\ncommand = ['uv', 'run', 'python', 'benchmark/benchmark.py']\n"
    )

    objective = _load_objective(load_input_bundle(bundle))
    assert "Maximize throughput" in objective


def test_cli_missing_objective_md_errors(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288

    bundle = tmp_path / "modelB"
    bundle.mkdir()
    (bundle / "vibesys.input.toml").write_text(
        "version = 1\n\n"
        "[agent]\ndomain = 'llm-serving'\n\n"
        "[accuracy]\ncommand = ['uv', 'run', 'python', 'accuracy_checker/checker.py']\n\n"
        "[benchmark]\ncommand = ['uv', 'run', 'python', 'benchmark/benchmark.py']\n"
    )

    with pytest.raises(FileNotFoundError, match="OBJECTIVE.md"):  # noqa: RUF043  # tracked: #288
        load_input_bundle(bundle)


def test_cli_rejects_modal_with_nsys_profiler(tmp_path, ref_file):  # noqa: ANN001, ANN201, ARG001  # tracked: #288
    """--modal only supports torch profiler."""
    from vibesys.main import _build_agent_parser, _validate_agent  # noqa: PLC0415  # tracked: #288

    parser = _build_agent_parser()
    validate_args = _validate_agent
    args = parser.parse_args(
        [
            "--input",
            str(Path(ref_file).parent),
            "--exp-name",
            "test",
            "--modal",
            "--profiler",
            "nsys",
        ]
    )
    with pytest.raises(ConfigurationError):
        validate_args(args)


# ---------------------------------------------------------------------------
# --resume semantics
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Roadmap + plateau detection
# ---------------------------------------------------------------------------


def test_ensure_roadmap_seeds_header_when_missing(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "roadmap.md"
    assert not p.exists()
    issue_board.ensure_roadmap_file(p)
    assert p.exists()
    text = p.read_text()
    # The seed must scaffold the four sections so the orchestrator's first
    # round starts with a clear structure.
    assert "## Major" in text
    assert "## Minor" in text
    assert "## Done" in text
    assert "## Abandoned" in text


def test_ensure_roadmap_does_not_overwrite_existing(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "roadmap.md"
    p.write_text("# my custom plan\n")
    issue_board.ensure_roadmap_file(p)
    assert p.read_text() == "# my custom plan\n"


def test_read_roadmap_returns_text(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "roadmap.md"
    p.write_text("hello\n")
    assert issue_board.read_roadmap(p) == "hello\n"


def test_read_roadmap_missing_returns_empty(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from vibesys.loops.agent import issue_board  # noqa: PLC0415  # tracked: #288

    p = tmp_path / "nope.md"
    assert issue_board.read_roadmap(p) == ""


def test_outer_prompts_reference_memory_paths_without_embedding_contents():  # noqa: ANN201  # tracked: #288
    template_dir = PROMPTS_DIR / "loops" / "agent"
    plan_prompt = (template_dir / "orchestrator_plan_prompt.j2").read_text()
    pre_prompt = (template_dir / "orchestrator_pre_round_prompt.j2").read_text()

    assert "progress_location" in plan_prompt
    assert "roadmap_location" in plan_prompt
    assert "pareto_archive_location" in plan_prompt
    assert "recent_progress_text" not in plan_prompt
    assert "roadmap_text" not in plan_prompt
    assert "pareto_archive_summary" not in plan_prompt
    assert "Cite" in plan_prompt
    assert "not stable text" in plan_prompt
    assert "under 4,000 output tokens" in plan_prompt
    assert "Once evidence clearly ranks one parent and mechanism" in plan_prompt
    assert "progress_location" in pre_prompt
    assert "recent_progress_text" not in pre_prompt

    for name in ("implementer_prompt.j2", "judge_prompt.j2", "single_agent_round_prompt.j2"):
        role_prompt = (template_dir / name).read_text()
        assert "pareto_archive_location" in role_prompt
        assert "pareto_archive_summary" not in role_prompt


@pytest.mark.parametrize(
    ("progress_name", "expected"),
    (("progress.md", "pareto-frontier.md"), ("progress", "progress/pareto-frontier.md")),  # noqa: PT007  # tracked: #288
)
def test_pareto_archive_is_materialized_beside_progress(tmp_path, progress_name, expected):  # noqa: ANN001, ANN201  # tracked: #288
    progress_path = tmp_path / progress_name

    document = issue_board.write_pareto_archive(progress_path, "Trusted frontier: round 4")

    assert document == tmp_path / expected
    assert document.read_text() == "# Pareto frontier\n\nTrusted frontier: round 4\n"


def _record(round_number: int, perf: float | None, unit: str = "tok/s"):  # noqa: ANN202  # tracked: #288
    """Build a RoundRecord shorthand for plateau tests."""
    return RoundRecord(
        round_number=round_number,
        commit=f"sha{round_number:03d}",
        perf_metric=perf,
        perf_unit=unit if perf is not None else None,
        passed=perf is not None,
        official_evaluation=perf is not None,
        official_evaluation_reason="cadence" if perf is not None else None,
    )


def test_detect_plateau_returns_none_when_too_few_rounds():  # noqa: ANN201  # tracked: #288
    from vibesys.loops.agent.loop import _detect_plateau  # noqa: PLC0415  # tracked: #288

    # Two rounds is below the 3-round minimum streak.
    records = [_record(1, 40.0), _record(2, 41.0)]
    assert _detect_plateau(records) is None


def test_detect_plateau_fires_on_flat_perf_streak():  # noqa: ANN201  # tracked: #288
    from vibesys.loops.agent.loop import _detect_plateau  # noqa: PLC0415  # tracked: #288

    # 41.0 vs 41.5 is ~1.2% spread — well under the 5% threshold.
    records = [_record(1, 41.0), _record(2, 41.5), _record(3, 41.2)]
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–3" in warning  # noqa: RUF001  # tracked: #288
    assert "tok/s" in warning


def test_detect_plateau_skips_when_perf_diverges():  # noqa: ANN201  # tracked: #288
    from vibesys.loops.agent.loop import _detect_plateau  # noqa: PLC0415  # tracked: #288

    # 41.0 vs 116.0 is ~64% spread — clearly off-plateau.
    records = [_record(1, 41.0), _record(2, 116.0), _record(3, 114.5)]
    assert _detect_plateau(records) is None


def test_detect_plateau_ignores_rounds_without_perf():  # noqa: ANN201  # tracked: #288
    """Rounds where the profiler skipped or the round failed (perf=None) must
    not interrupt the streak — only valid measurements count."""
    from vibesys.loops.agent.loop import _detect_plateau  # noqa: PLC0415  # tracked: #288

    records = [
        _record(1, 41.0),
        _record(2, None),  # profiler skipped or failed round
        _record(3, 41.3),
        _record(4, 41.1),
    ]
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–4" in warning  # noqa: RUF001  # tracked: #288


def test_detect_plateau_ignores_failed_official_measurements():  # noqa: ANN201  # tracked: #288
    """A measured row rejected by the judge or another round gate is not
    trusted trajectory evidence, even when the framework evaluator ran."""
    from vibesys.loops.agent.loop import _detect_plateau  # noqa: PLC0415  # tracked: #288

    failed = _record(2, 100.0)
    failed.passed = False
    records = [
        _record(1, 41.0),
        failed,
        _record(3, 41.3),
        _record(4, 41.1),
    ]
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–4" in warning  # noqa: RUF001  # tracked: #288


def test_failed_official_measurement_cannot_complete_plateau_streak():  # noqa: ANN201  # tracked: #288
    from vibesys.loops.agent.loop import _detect_plateau  # noqa: PLC0415  # tracked: #288

    failed = _record(3, 41.1)
    failed.passed = False
    assert _detect_plateau([_record(1, 41.0), _record(2, 41.2), failed]) is None


def test_detect_plateau_streak_must_be_recent():  # noqa: ANN201  # tracked: #288
    """A plateau early in the run that's followed by a clear win must NOT
    fire a warning on the next round — only the *last N* matter."""
    from vibesys.loops.agent.loop import _detect_plateau  # noqa: PLC0415  # tracked: #288

    records = [
        _record(1, 41.0),  # plateau
        _record(2, 41.2),  # plateau
        _record(3, 41.1),  # plateau (would fire here)
        _record(4, 116.0),  # break
    ]
    # By round 4, the recent streak (rounds 2,3,4) spans 41.2-116.0 → no plateau.
    assert _detect_plateau(records) is None


def test_loop_creates_roadmap_md_in_workspace(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """The first round of a fresh run must seed roadmap.md in the workspace."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build server",
                pass_criteria="/health 200",  # noqa: S106  # tracked: #288
                reasoning="cold start",
            ),
        ],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert result is True
    # The workspace lives under exp_env/<run-dir>/workspace/.
    roadmap_files = list((tmp_path / "exp_env").glob("*/workspace/roadmap.md"))
    assert len(roadmap_files) == 1
    text = roadmap_files[0].read_text()
    assert "## Major" in text


def test_loop_can_create_scannable_directory_memory(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    runner = _make_orchestrate_runner()

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=1,
        memory_layout="directories",
    )

    assert result is True
    workspaces = list((tmp_path / "exp_env").glob("*/workspace"))
    workspace = workspaces[0]
    assert (workspace / "roadmap" / "index.md").exists()
    round_log = workspace / "progress" / "round-0001.md"
    assert round_log.exists()
    assert "Framework" not in round_log.read_text()  # stub skips official commands


def test_loop_threads_roadmap_into_orchestrator_prompt(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """The orchestrator's plan prompt must include the current roadmap.md
    contents so the orchestrator can update them."""
    seen_prompts: list[str] = []
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(task="t", pass_criteria="p", reasoning="r"),  # noqa: S106  # tracked: #288
        ],
    )
    real = runner.invoke.side_effect

    def spy(*, kind, response_cls, **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            seen_prompts.append(kwargs.get("system_prompt", ""))
        return real(kind=kind, response_cls=response_cls, **kwargs)

    runner.invoke.side_effect = spy

    _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert len(seen_prompts) == 1
    prompt = seen_prompts[0]
    # Roadmap section header must be present, and so must the seed scaffold.
    assert "Roadmap" in prompt
    assert "Major" in prompt
    assert "roadmap.md" in prompt


def test_loop_threads_plateau_warning_into_prompt(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """When the prior rounds plateau on perf, the orchestrator's next prompt
    must include the plateau warning."""
    seen_prompts: list[str] = []
    # Five rounds: round 1 is cold-start (no profiler), rounds 2-4 produce
    # flat perf metrics, and round 5 is the round under test (its plan call
    # should see the plateau warning).
    plans = [
        OrchestratorPlan(task=f"r{i}", pass_criteria="p", reasoning=f"r{i}")  # noqa: S106  # tracked: #288
        for i in range(1, 6)  # noqa: RUF100, S106  # tracked: #288
    ]
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="x", reasoning="ok"),
        ]
        * 4,  # rounds 2-5
        plans=plans,
        profiler_responses=[
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=42.0,
                perf_unit="tok/s",
            ),
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=42.1,
                perf_unit="tok/s",
            ),
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=41.9,
                perf_unit="tok/s",
            ),
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=42.05,
                perf_unit="tok/s",
            ),
        ],
        implementer_perf_metrics=[None, 42.0, 42.1, 41.9, 42.05],
    )
    real = runner.invoke.side_effect

    def spy(*, kind, response_cls, **kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            seen_prompts.append(kwargs.get("system_prompt", ""))
        return real(kind=kind, response_cls=response_cls, **kwargs)

    runner.invoke.side_effect = spy

    _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=5,
        official_eval_every=1,
    )
    assert len(seen_prompts) == 5
    # Rounds 1-4 have <3 valid perf records before each plan call → no
    # warning yet (round 1: 0 perf; round 2: 0 perf; round 3: 1 perf; round 4: 2 perf).
    for i in range(4):
        assert "Plateau detected" not in seen_prompts[i], (
            f"round {i + 1} should not yet have plateau warning"
        )
    # Round 5 plan call sees rounds 1-4 in records (3 valid perf measurements
    # from rounds 2,3,4 — flat at 41.9-42.1) → warning fires.
    assert "Plateau detected" in seen_prompts[4]
    assert "Refresh an analytical performance model" in seen_prompts[4]
    assert "unexplained residual" in seen_prompts[4]


def test_loop_resume_with_round_number_starts_there(tmp_path, ref_file):  # noqa: ANN001, ANN201  # tracked: #288
    """--resume 4 starts the loop at round 4 (prior rounds were committed by previous run)."""
    # With start_round=4 and max_rounds=5 only rounds 4 and 5 execute.
    plans = [
        OrchestratorPlan(task="keep going", pass_criteria="tests pass", reasoning="round 4"),  # noqa: S106  # tracked: #288
        OrchestratorPlan(task="more work", pass_criteria="tests pass", reasoning="round 5"),  # noqa: S106  # tracked: #288
    ]
    runner = _make_orchestrate_runner(plans=plans)

    # Pre-seed an existing exp dir so the context init takes the `existing=True`
    # branch.
    exp_env = tmp_path / "exp_env"
    (exp_env / "20260422-000000-test-orch").mkdir(parents=True)
    # Minimal git setup so the context validation accepts the repo.
    import subprocess  # noqa: PLC0415  # tracked: #288

    subprocess.run(
        ["git", "init"],  # noqa: S607  # tracked: #288
        cwd=exp_env / "20260422-000000-test-orch",
        capture_output=True,
        check=True,
    )
    ws = exp_env / "20260422-000000-test-orch" / "workspace"
    ws.mkdir()
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True)  # noqa: S607  # tracked: #288
    (ws / "dummy.txt").write_text("x")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "add", "-A"], cwd=ws, env={**env}, capture_output=True, check=True)  # noqa: S607  # tracked: #288
    subprocess.run(
        ["git", "commit", "-m", "seed"],  # noqa: S607  # tracked: #288
        cwd=ws,
        env={**env},
        capture_output=True,
        check=True,  # noqa: RUF100, S607  # tracked: #288
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        exp_name="20260422-000000-test-orch",
        existing=True,
        start_round=4,
        max_rounds=5,
    )
    assert result is True
    # Round 4 and 5 only: 2 plan calls (one task, one done).
    assert runner.counters["orch_plan"] == 2
